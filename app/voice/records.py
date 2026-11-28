"""Persistence for voice proposals (Step O, slices 2-3).

A slice-1 :class:`app.voice.proposals.VoiceActionProposal` is an in-memory
dataclass; this module stores it as a :class:`app.db.voice_models.VoiceProposal`
row and renders it for the API.

Status rules (deliberate, see docs/step-o-voice-plan.md):
* The row's ``status`` is written only by the execution path (``executed`` /
  ``failed``). While a proposal waits for its platform approval, the API derives
  the display status from the linked :class:`app.platform.models.PlatformApprovalRequest`
  (``proposed`` -> ``approved`` / ``rejected`` / ``expired``), so no workflow
  step ever has to write this row just to report progress.
* The transcript is stored only as a short excerpt (<=500 chars, slice 1); audio
  is never stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.voice_models import VoiceProposal
from app.platform.models import PlatformApprovalRequest
from app.voice.proposals import VoiceActionProposal

# Statuses the row itself carries (execution outcomes). The display status can
# also be 'approved' / 'rejected' / 'expired', derived from the approval row.
DISPLAY_STATUSES = ("proposed", "approved", "rejected", "expired", "executed", "failed")


async def create_voice_proposal_record(
    db,
    proposal: VoiceActionProposal,
    *,
    group_id: uuid.UUID,
    scope_id: uuid.UUID,
) -> VoiceProposal:
    """Store a slice-1 proposal. Flushes only; the caller commits."""
    row = VoiceProposal(
        id=proposal.id,
        group_id=group_id,
        scope_id=scope_id,
        requested_by=proposal.requested_by,
        intent_kind=proposal.intent_kind,
        action_type=proposal.action_spec.type,
        action_hash=proposal.action_hash,
        action=proposal.action_spec.canonical(),
        subject=proposal.subject,
        status="proposed",
        transcript_excerpt=proposal.transcript_excerpt,
        approval_id=None,
        wf_run_id=None,
        expires_at=proposal.expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def approval_status(db, approval_id: uuid.UUID | None) -> str | None:
    """The linked platform approval's status, or ``None`` when there is none."""
    if approval_id is None:
        return None
    row = await db.get(PlatformApprovalRequest, approval_id)
    return row.status if row is not None else None


def display_status(row: VoiceProposal, approval: str | None) -> str:
    """What the founder sees. Execution outcomes win; otherwise the approval
    row decides; a proposal whose own expiry passed without a decision is
    ``expired``."""
    if row.status in {"executed", "failed"}:
        return row.status
    if approval in {"approved", "consumed"}:
        return "approved"
    if approval == "rejected":
        return "rejected"
    if approval == "expired" or datetime.now(UTC) >= row.expires_at:
        return "expired"
    return "proposed"


async def load_proposal(db, proposal_id: uuid.UUID) -> VoiceProposal | None:
    return await db.get(VoiceProposal, proposal_id)


async def proposal_out(db, row: VoiceProposal) -> dict:
    """API shape for one proposal, with the derived status and the linked
    approval / workflow run ids. Never includes the transcript beyond the
    stored excerpt, and never includes audio (none is stored)."""
    approval = await approval_status(db, row.approval_id)
    return {
        "id": str(row.id),
        "intent_kind": row.intent_kind,
        "action_type": row.action_type,
        "action_hash": row.action_hash,
        "subject": row.subject,
        "status": display_status(row, approval),
        "transcript_excerpt": row.transcript_excerpt,
        "approval_id": str(row.approval_id) if row.approval_id else None,
        "approval_status": approval,
        "wf_run_id": str(row.wf_run_id) if row.wf_run_id else None,
        "expires_at": row.expires_at.isoformat(),
        "execution_result": row.execution_result,
        "failure_reason": row.failure_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
    }


async def proposals_for_group(db, group_id: uuid.UUID, *, limit: int = 50) -> list[VoiceProposal]:
    rows = (await db.scalars(select(VoiceProposal).where(VoiceProposal.group_id == group_id)
                             .order_by(VoiceProposal.created_at.desc()).limit(limit))).all()
    return list(rows)
