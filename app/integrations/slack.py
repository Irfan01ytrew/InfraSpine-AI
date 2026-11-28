"""Slack connector for Step V wider integrations (mock-transport based).

Runs on an injected transport, so tests and this preparation slice never
touch the real Slack API. Validation reuses the existing ``SlackMessage``
schema through ``app.integrations.adapters.validate_operation``; inbound
events are normalised with ``registry.normalize_inbound_event`` and are
always untrusted.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.errors import InvalidInputError
from app.integrations import adapters, registry

_SLACK_BASE = "https://slack.com/api"


class SlackConnector:
    """Slack ``verify`` + ``slack.send`` + inbound event normalisation."""

    provider = "slack"

    def __init__(self, transport, *, token: str = "mock-token"):
        self.transport = transport
        self.token = token

    async def verify(self) -> dict:
        """Call ``auth.test``; returns team/user. ``ok: false`` raises."""
        result = await self.transport.get("/auth.test")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise InvalidInputError("Slack verification failed.")
        return {"ok": True, "team": result.get("team"), "user": result.get("user")}

    async def send_message(self, channel: str, text: str) -> dict:
        """Validate then post one message; returns the provider timestamp."""
        connector = SimpleNamespace(provider="slack", config={"scopes": ["chat:write"]})
        payload = adapters.validate_operation(connector, "slack.send", {"channel": channel, "text": text})
        result = await self.transport.post("/chat.postMessage", payload)
        if not isinstance(result, dict) or result.get("ok") is not True or not result.get("ts"):
            raise InvalidInputError("Slack send failed.")
        return {"ok": True, "channel": channel, "ts": result["ts"]}

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound Slack event envelope. Always untrusted."""
        return registry.normalize_inbound_event("slack", raw)

    @staticmethod
    def base_url() -> str:
        return _SLACK_BASE
