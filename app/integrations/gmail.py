"""Gmail connector class for Step V wider integrations (mock-transport based).

Runs on an injected transport, so tests and this slice never touch the real
Gmail API. Read paths (``verify`` / list) need no approval; **send is
approval-gated**: ``send_message`` refuses to call the provider unless it is
given an approval whose digest covers the exact validated action and whose
expiry is still in the future — mirroring the worker's
``approval_required`` rule (a write without a live approval never executes).
Gmail-specific provider errors are mapped to typed errors (quota/rate-limit
vs invalid grant) instead of a generic failure.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace

from app.core.errors import InvalidInputError
from app.integrations import adapters, registry
from app.integrations.security import digest

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"


class ApprovalRequiredError(InvalidInputError):
    """Raised when a Gmail send has no live approval covering the exact action."""


class GmailAuthError(InvalidInputError):
    """Raised when Gmail rejects the credentials (e.g. invalid_grant)."""


class GmailRateLimitedError(InvalidInputError):
    """Raised when Gmail reports quota exhaustion / rate limiting (429)."""


def _error_from(result: object):
    """Map a Gmail error envelope to a typed exception, or None."""
    if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
        return None
    error = result["error"]
    code = error.get("code")
    message = str(error.get("message", "")).lower()
    if code == 429 or "quota" in message or "ratelimitexceeded" in message:
        return GmailRateLimitedError("Gmail quota/rate limit exceeded.")
    if code == 401 or "invalid_grant" in message:
        return GmailAuthError("Gmail credentials rejected.")
    return InvalidInputError("Gmail request failed.")


class GmailConnector:
    """Gmail ``verify`` + read + approval-gated ``email.send``."""

    provider = "gmail"

    def __init__(self, transport, *, token: str = "mock-token"):
        self.transport = transport
        self.token = token

    async def verify(self) -> dict:
        """Call ``/users/me/profile``; returns the mailbox address + totals."""
        result = await self.transport.get("/users/me/profile")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not result.get("emailAddress"):
            raise InvalidInputError("Gmail verification failed.")
        return {
            "ok": True,
            "emailAddress": result.get("emailAddress"),
            "messagesTotal": result.get("messagesTotal"),
        }

    async def list_messages(self, *, query: str | None = None, max_results: int = 10) -> dict:
        """List message ids (read-only). ``query`` is a Gmail search string."""
        if not 1 <= max_results <= 100:
            raise InvalidInputError("max_results must be between 1 and 100.")
        path = f"/users/me/messages?maxResults={max_results}"
        if query:
            if len(query) > 500:
                raise InvalidInputError("Gmail search query is too long.")
            path += f"&q={query}"
        result = await self.transport.get(path)
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise InvalidInputError("Gmail list failed.")
        return {"messages": result["messages"], "nextPageToken": result.get("nextPageToken")}

    def _check_approval(self, validated: dict, approval: object) -> None:
        """The approval must digest-cover this exact action and be unexpired."""
        expected = digest({"operation": "email.send", "payload": validated})
        if not isinstance(approval, dict):
            raise ApprovalRequiredError("Gmail send needs an approval: approval_required.")
        if approval.get("digest") != expected:
            raise ApprovalRequiredError("Approval does not cover this exact action.")
        expires_at = approval.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
            raise ApprovalRequiredError("Approval expired or missing expiry.")

    async def send_message(self, to: list[str], subject: str, text: str, *, approval: object = None) -> dict:
        """Validate, check the approval, then send one message. Never auto-sends."""
        connector = SimpleNamespace(
            provider="gmail",
            config={"scopes": ["https://www.googleapis.com/auth/gmail.send"]},
        )
        payload = adapters.validate_operation(
            connector, "email.send", {"to": to, "subject": subject, "text": text}
        )
        self._check_approval(payload, approval)
        message = EmailMessage()
        message["To"] = ", ".join(payload["to"])
        message["Subject"] = payload["subject"]
        message.set_content(payload["text"])
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = await self.transport.post("/users/me/messages/send", {"raw": raw})
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not result.get("id"):
            raise InvalidInputError("Gmail send failed.")
        return {"ok": True, "id": result["id"]}

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound Gmail push event. Always untrusted."""
        return registry.normalize_inbound_event("gmail", raw)

    @staticmethod
    def base_url() -> str:
        return _GMAIL_BASE
