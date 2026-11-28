"""WhatsApp approvals (Step F slice 5).

When an Agent Studio run is ``awaiting_approval``, the approver gets a card
on the same WhatsApp channel — interactive Approve/Reject buttons on Cloud,
``APPROVE``/``REJECT`` text commands on Baileys — bound to the exact action
hash (``app.platform.approvals.ActionSpec``). The reply is verified as coming
from the run's owner (identity from ``app.platform.channels``); a forged,
replayed or wrong-sender approval is refused. The decision is applied through
``AgentStudioService.decide`` — exactly as the web UI decision — single use,
1-hour expiry.

Reply wording: "Done (verified)" is stamped only when a verified
``execute_once`` record exists for the run's attempt; a model utterance of
"done" without a verify result never counts as verification.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_studio.service import AgentStudioService
from app.agent_studio.service import _open as _open_sealed
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioApproval, AgentStudioRun
from app.db.models import User
from app.platform.approvals import ActionSpec
from app.platform.models import PlatformActionExecution
from app.services import audit
from app.services.audit import AuditContext

# Button reply ids sent on Cloud interactive cards, and the text commands
# accepted on Baileys. The 12-hex-char suffix is the first 12 chars of the
# action hash, so the decision is bound to the exact action the card showed.
APPROVE_BUTTON_PREFIX = "wa-approve:"
REJECT_BUTTON_PREFIX = "wa-reject:"
_HASH12 = r"[0-9a-f]{12}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_BUTTON_RE = re.compile(
    rf"^wa-(approve|reject):({_UUID})(?::({_HASH12}))?$", re.IGNORECASE
)
_TEXT_RE = re.compile(
    rf"^\s*(approve|reject)\s+({_UUID})(?:\s+({_HASH12}))?\s*$", re.IGNORECASE
)

APPROVAL_TTL_NOTE = "Single use — it expires in 1 hour. Only the run's owner can decide."


def parse_approval_command(text: Any) -> tuple[str, uuid.UUID, str | None] | None:
    """Parse a Cloud button reply id or a Baileys APPROVE/REJECT command.

    Returns ``(decision, approval_id, presented_hash12)`` or ``None`` when the
    text is not an approval command. The hash is optional here so the caller
    can refuse a command that omits it with a specific message; a decision
    is never applied without it.
    """
    if not isinstance(text, str):
        return None
    match = _BUTTON_RE.match(text.strip()) or _TEXT_RE.match(text)
    if match is None:
        return None
    try:
        approval_id = uuid.UUID(match.group(2))
    except ValueError:
        return None
    presented = match.group(3).lower() if match.group(3) else None
    return match.group(1).lower(), approval_id, presented


def _sanitize(value: Any) -> Any:
    """Make an arbitrary tool-argument value hashable by ActionSpec.

    ActionSpec's canonical form rejects floats and unknown types; the same
    sanitizer runs at card time and at decision time, so the hash is stable.
    """
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def decode_approval_action(cipher, approval: AgentStudioApproval) -> dict[str, Any]:
    """The pending tool action sealed on the approval row (raises on tamper)."""
    raw = _open_sealed(
        cipher, "approval-action", approval.id,
        approval.action_ciphertext, approval.action_nonce, approval.action_key_id,
    )
    action = json.loads(raw)
    if not isinstance(action, dict):
        raise ValueError("approval action is not an object")
    return action


def approval_action_hash(cipher, approval: AgentStudioApproval, scope_id: uuid.UUID) -> str:
    """The exact-action hash for an approval, via ``app.platform.approvals``."""
    action = decode_approval_action(cipher, approval)
    spec = ActionSpec(
        "agent_studio.approve",
        scope_id,
        {
            "run_id": str(approval.run_id),
            "approval_id": str(approval.id),
            "tool": str(action.get("name") or approval.tool_name),
            "arguments": _sanitize(action.get("arguments") or {}),
        },
        subject=f"agent_studio_run:{approval.run_id}",
    )
    return spec.hash()


def approval_summary(tool_name: str, action: dict[str, Any]) -> str:
    """The same one-line action summary the web UI shows (``public_run``)."""
    if tool_name == "create_file":
        fmt = str(((action.get("arguments") or {}).get("format") or "document")).upper()
        return f"Create a downloadable {fmt} file."
    if tool_name == "generate_image":
        return "Generate an image using the configured AI provider."
    return "Run an action that requires your approval."


def approval_card_text(
    *, tool_name: str, summary: str, approval_id: uuid.UUID, action_hash: str,
) -> str:
    """The approval card body (Cloud interactive body and Baileys text)."""
    code = action_hash[:12]
    return (
        "*Approval required*\n\n"
        f"Tool: {tool_name}\n"
        f"{summary}\n"
        f"Action code: {code}\n\n"
        "Reply with exactly one of these:\n"
        f"APPROVE {approval_id} {code}\n"
        f"REJECT {approval_id} {code}\n\n"
        f"{APPROVAL_TTL_NOTE}"
    )


def approval_buttons(approval_id: uuid.UUID, action_hash: str) -> list[tuple[str, str]]:
    """Meta interactive reply buttons: (reply id, title)."""
    code = action_hash[:12]
    return [
        (f"{APPROVE_BUTTON_PREFIX}{approval_id}:{code}", "Approve"),
        (f"{REJECT_BUTTON_PREFIX}{approval_id}:{code}", "Reject"),
    ]


def _audit_refusal(
    db: AsyncSession, *, approval_id: uuid.UUID, channel: str, reason: str, user_id: uuid.UUID | None,
) -> None:
    audit.record(
        db, AuditContext(user_id, None, None, None), "integration.whatsapp_approval_refused",
        outcome="failure", target=str(approval_id),
        details={"channel": channel, "reason": reason},
    )


async def decide_from_whatsapp(
    db: AsyncSession,
    *,
    principal_user_id: uuid.UUID,
    approval_id: uuid.UUID,
    decision: str,
    presented_hash: str | None,
    cipher,
    settings,
    runtime,
    rag,
    channel: str,
) -> str:
    """Apply a WhatsApp approval decision; returns the reply text.

    Refusals never raise — they return a safe reply and are audited. The
    decision itself goes through ``AgentStudioService.decide``, exactly as
    the web UI decision (single use, 1-hour expiry, action-match check).
    """
    approval = await db.get(AgentStudioApproval, approval_id)
    run = await db.get(AgentStudioRun, approval.run_id) if approval is not None else None
    user = await db.get(User, principal_user_id)
    if approval is None or run is None or user is None or not user.is_active:
        _audit_refusal(db, approval_id=approval_id, channel=channel, reason="not_found",
                       user_id=principal_user_id)
        return ("I couldn't find that approval. Open InfraSpine AI → AI Agents → "
                "Approvals & execution log to review it.")
    if run.owner_id != user.id:
        _audit_refusal(db, approval_id=approval_id, channel=channel, reason="wrong_sender",
                       user_id=principal_user_id)
        return "This approval belongs to a different account. Only the run's owner can decide it."
    if approval.status != "pending" or run.status != "awaiting_approval":
        _audit_refusal(db, approval_id=approval_id, channel=channel, reason="already_decided",
                       user_id=principal_user_id)
        return "This approval was already decided. Open the Approvals log to see the result."
    from app.platform.side_effects import org_scope_id

    try:
        scope_id = await org_scope_id(db)
        action_hash = approval_action_hash(cipher, approval, scope_id)
    except Exception:
        _audit_refusal(db, approval_id=approval_id, channel=channel, reason="unverifiable",
                       user_id=principal_user_id)
        return "I couldn't verify that approval card. Open the Approvals log to review it."
    if presented_hash is None:
        return ("Reply with the full command from the approval card — it includes a "
                "verification code after the approval id.")
    if presented_hash.lower() != action_hash[:12]:
        _audit_refusal(db, approval_id=approval_id, channel=channel, reason="hash_mismatch",
                       user_id=principal_user_id)
        return ("The action changed since the approval card was sent. Open the Approvals "
                "log to review the current action.")
    service = AgentStudioService(db, user, settings, cipher, runtime, rag)
    try:
        await service.decide(run.id, decision, f"Decided over WhatsApp ({channel}).", user)
    except NotFoundError:
        return ("I couldn't find that approval. Open InfraSpine AI → AI Agents → "
                "Approvals & execution log to review it.")
    except ConflictError as exc:
        message = str(exc)
        if "expired" in message.lower():
            return "This approval expired. Reject it in the Approvals log or start a fresh run."
        return "This approval was already decided. Open the Approvals log to see the result."
    except PermissionDeniedError:
        return "This approval belongs to a different account. Only the run's owner can decide it."
    audit.record(
        db, AuditContext(user.id, None, None, None), f"agent_studio.action_{decision}d",
        target=str(approval.id),
        details={"channel": channel, "action_hash": action_hash[:12]},
    )
    if decision == "approve":
        return "Approved. The agent will continue now — I'll send the result here when it's done."
    return "Rejected. The agent will not run that action."


async def run_verified(db: AsyncSession, run_id: uuid.UUID | None) -> bool:
    """True when an ``execute_once`` record proves this run's attempt verified.

    The unified engine dispatches each attempt through ``execute_once`` (key
    ``agent_studio:<run>:attempt:<n>``) and marks the ledger row ``verified``
    only after reading the run back. A run with no such row — an inline
    attempt, or an answer that merely says "done" — is not verified. A run
    without an id (a test double) has no ledger row either.
    """
    if run_id is None:
        return False
    row = await db.scalar(
        select(PlatformActionExecution.id).where(
            PlatformActionExecution.idempotency_key.like(f"agent_studio:{run_id}:attempt:%"),
            PlatformActionExecution.status == "verified",
        ).limit(1)
    )
    return row is not None


async def completed_reply(db: AsyncSession, run: AgentStudioRun, answer: str) -> str:
    """The completed-run reply: "Done (verified)" only on a verified completion."""
    if await run_verified(db, getattr(run, "id", None)):
        return f"Done (verified)\n\n{answer}"
    return answer
