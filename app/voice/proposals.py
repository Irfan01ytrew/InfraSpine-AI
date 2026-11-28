"""Voice action proposals: a voice note proposes an exact action; nothing runs
without a consumed platform approval for exactly that action.

Slice 1 executes nothing. :func:`dry_run_voice_action` only describes what slice 2
will dispatch as a unified workflow run (``origin='voice'``) after the approval
gate passes. The gate reuses the platform approval machinery
(:mod:`app.platform.approvals`): the exact-action hash, expiry, single use (enforced
by ``consume_approval`` when the grant is issued), and separation of duties.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.errors import ConflictError, InvalidInputError, PermissionDeniedError
from app.platform.approvals import ActionSpec, ApprovalGrant
from app.voice.intents import VoiceIntent

DEFAULT_VOICE_PROPOSAL_TTL = timedelta(hours=1)
MAX_TRANSCRIPT_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class VoiceActionProposal:
    """A proposed voice action. ``status`` stays ``proposed`` until slice 2 dispatches it."""

    id: uuid.UUID
    intent_kind: str
    scope_id: uuid.UUID
    requested_by: uuid.UUID
    action_spec: ActionSpec
    action_hash: str
    status: str
    expires_at: datetime
    transcript_excerpt: str
    subject: str | None


@dataclass(frozen=True)
class VoiceDryRunResult:
    """What slice 2 would dispatch. No side effect has happened."""

    proposal_id: uuid.UUID
    action_type: str
    action_hash: str
    status: str
    would_dispatch: str


def _sanitized_params(params: dict) -> dict:
    """Only plain values may enter an ActionSpec; anything else is dropped."""
    clean: dict = {}
    for key, value in params.items():
        if isinstance(value, (str, int, bool)) or value is None:
            clean[str(key)] = value
    return clean


def propose_voice_action(
    intent: VoiceIntent,
    *,
    scope_id: uuid.UUID,
    requested_by: uuid.UUID,
    transcript: str,
    ttl: timedelta = DEFAULT_VOICE_PROPOSAL_TTL,
) -> VoiceActionProposal:
    """Turn a recognised intent into a proposal bound to an exact action hash.

    Raises :class:`InvalidInputError` for ``unknown`` intents: no recognised
    intent means no proposal, never a best-guess action.
    """
    if intent.kind == "unknown":
        raise InvalidInputError(
            "This voice note does not ask for a recognised action."
        )
    spec = ActionSpec(
        type=f"voice.{intent.kind}",
        scope_id=scope_id,
        params=_sanitized_params(intent.params),
        subject=intent.subject,
    )
    excerpt = (transcript or "").strip()[:MAX_TRANSCRIPT_EXCERPT_CHARS]
    return VoiceActionProposal(
        id=uuid.uuid4(),
        intent_kind=intent.kind,
        scope_id=scope_id,
        requested_by=requested_by,
        action_spec=spec,
        action_hash=spec.hash(),
        status="proposed",
        expires_at=datetime.now(UTC) + ttl,
        transcript_excerpt=excerpt,
        subject=intent.subject,
    )


def assert_voice_execution_allowed(
    proposal: VoiceActionProposal,
    approval: ApprovalGrant | None,
    *,
    now: datetime | None = None,
) -> None:
    """Raise unless this exact proposal may run under this approval grant.

    The grant itself is issued by :func:`app.platform.approvals.consume_approval`,
    which enforces single use and expiry on the stored approval row; this gate
    additionally checks the hash match, the proposal's own expiry, and that the
    approver is not the requester (separation of duties).
    """
    if approval is None:
        raise PermissionDeniedError("This voice action needs an approval before it can run.")
    if approval.action_hash != proposal.action_hash:
        raise ConflictError("The approval covers a different action (parameters or amount changed).")
    if approval.approved_by == proposal.requested_by:
        raise PermissionDeniedError(
            "Separation of duties: you cannot approve your own voice request."
        )
    current = now or datetime.now(UTC)
    if current >= proposal.expires_at:
        raise ConflictError("This voice proposal expired before it was used. Record a new voice note.")


def dry_run_voice_action(
    proposal: VoiceActionProposal,
    *,
    approval: ApprovalGrant | None,
    now: datetime | None = None,
) -> VoiceDryRunResult:
    """Pass the approval gate and describe what would be dispatched.

    Executes nothing. Slice 2 replaces this description with a real unified
    workflow run (``origin='voice'``) through ``execute_once``.
    """
    assert_voice_execution_allowed(proposal, approval, now=now)
    return VoiceDryRunResult(
        proposal_id=proposal.id,
        action_type=proposal.action_spec.type,
        action_hash=proposal.action_hash,
        status="dry_run_ready",
        would_dispatch=(
            f"Dispatch unified workflow run origin='voice' for {proposal.action_spec.type} "
            f"(subject={proposal.subject or 'none'}) via execute_once after approval "
            f"{approval.approval_id}."
        ),
    )
