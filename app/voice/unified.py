"""Voice proposals on the unified workflow runtime (Step O, slices 2-3).

A persisted voice proposal gets one workflow run (``origin='voice'``,
``origin_ref=<proposal id>``) in the founder's department:

    start -> approval (waits for the independent decision on the platform
             approval the route requested, /api/approvals/{id}/decide)
          -> execute  (``voice.manage``; the platform approval is consumed
                       exactly once inside ``execute_once`` with read-back of
                       the proposal row) -> end

Why a dedicated approval handler instead of the generic ``platform.approval``
step: the route requests ONE platform approval bound to the proposal's exact
action hash (slice 1). The generic step would request a *second* approval
whose hash also covers the run/step ids, leaving the route's approval orphaned
and the approver looking at two requests for one action. This handler waits on
the single route-created approval instead — same machinery
(``request/decide/consume``), one approval, one hash. The engine re-polls the
step (``poll_seconds=15``); the generic decide endpoint wakes runs only when
the run id is in the action params, which it is not here.

Single-use semantics: the approval is consumed inside ``execute_once`` together
with the execution claim. If the effect then fails, the approval stays spent —
the founder records a new voice note for a new approval. The proposal is
marked ``failed`` with the reason; it is never retried on the spent approval.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import String, and_, func, select

from app.core.errors import AppError, ConflictError, InvalidInputError, PermissionDeniedError
from app.db.models import User
from app.db.voice_models import VoiceProposal
from app.db.workflow_engine_models import WorkflowRun
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformActionExecution, PlatformApprovalRequest
from app.services.audit import AuditContext, record
from app.voice.effects import apply_voice_effect
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition

log = logging.getLogger(__name__)

TEMPLATE_REF = "voice.proposals"
TEMPLATE_NAME = "Voice action proposals"
TEMPLATE_DESCRIPTION = ("Founder voice notes on the unified runtime: independent approval, "
                        "then execution through execute_once, verified by read-back.")
DUTY = "voice.manage"


def voice_spec() -> dict:
    values = {"proposal_id": "$input.proposal_id"}
    return Definition.model_validate({
        "source": "template",
        "input_form": [{"name": "proposal_id", "label": "Voice proposal", "type": "text", "required": True}],
        "nodes": [
            {"id": "start", "type": "start", "label": "Voice note received", "x": 80, "y": 160},
            {"id": "approval", "type": "action", "label": "Independent approval", "x": 300, "y": 160,
             "risk": "medium", "timeout_seconds": 60, "max_attempts": 5,
             "config": {"handler": "voice.proposal_approval", "auto": True, "values": values}},
            {"id": "execute", "type": "action", "label": "Execute approved voice action", "x": 520, "y": 160,
             "risk": "high", "timeout_seconds": 120, "max_attempts": 5,
             "config": {"handler": "voice.proposal_execute", "permission": DUTY, "auto": True,
                        "values": values}},
            {"id": "end", "type": "end", "label": "Executed and verified", "x": 740, "y": 160},
        ],
        "edges": [{"source": "start", "target": "approval"}, {"source": "approval", "target": "execute"},
                  {"source": "execute", "target": "end"}],
    }).model_dump(mode="json")


async def ensure_voice_definition(db, *, scope_id, owner_id):
    from app.workflows.unified import ensure_system_definition
    return await ensure_system_definition(
        db, scope_id=scope_id, owner_id=owner_id, origin="voice", origin_ref=TEMPLATE_REF,
        name=TEMPLATE_NAME, description=TEMPLATE_DESCRIPTION, spec=voice_spec())


async def mirror_voice_proposal(db, cipher, proposal: VoiceProposal) -> WorkflowRun | None:
    """One unified run per voice proposal. Idempotent: a second call returns
    the existing run. Returns ``None`` when the department turned the unified
    path off (definition disabled); the pending sweep retries later."""
    from app.workflows.service import WorkflowService
    from app.workflows.unified import origin_run
    existing = await origin_run(db, "voice", proposal.id)
    if existing is not None:
        if proposal.wf_run_id is None:
            proposal.wf_run_id = existing.id
            await db.flush()
        return existing
    requester = await db.get(User, proposal.requested_by)
    if requester is None or not requester.is_active:
        return None
    definition = await ensure_voice_definition(db, scope_id=proposal.group_id, owner_id=requester.id)
    if not definition.enabled:
        return None
    service = WorkflowService(db, requester, cipher, AuditContext(requester.id, requester.email, None, None))
    run = await service.trigger(proposal.group_id, definition.id, {"proposal_id": str(proposal.id)},
                                f"voice:{proposal.id}", origin="voice", origin_ref=str(proposal.id))
    proposal.wf_run_id = run.id
    await db.flush()
    return run


async def mirror_new_proposal(db, cipher, proposal: VoiceProposal) -> None:
    """Best effort right after the route created a proposal; the engine's sweep
    catches anything missed."""
    try:
        async with db.begin_nested():
            await mirror_voice_proposal(db, cipher, proposal)
        await db.commit()
    except Exception as exc:  # never break intake because the runtime could not record the run
        log.info("Voice proposal %s not mirrored yet (%s)", proposal.id, type(exc).__name__)


_UNMIRRORABLE: set[uuid.UUID] = set()


async def mirror_pending_voice_proposals(sessionmaker, cipher, *, limit: int = 25) -> int:
    """Attach proposals that have no unified run yet (route mirror missed, or the
    definition was disabled when they were created). Expired proposals are
    never mirrored: their approval can no longer be decided."""
    count = 0
    async with sessionmaker() as db:
        query = (select(VoiceProposal).outerjoin(WorkflowRun, and_(
            WorkflowRun.origin == "voice", WorkflowRun.origin_ref == VoiceProposal.id.cast(String),
        )).where(WorkflowRun.id.is_(None), VoiceProposal.status.in_(["proposed", "approved"]),
                 VoiceProposal.expires_at > datetime.now(UTC)))
        if _UNMIRRORABLE:
            query = query.where(VoiceProposal.id.not_in(list(_UNMIRRORABLE)[-5000:]))
        for proposal in (await db.scalars(query.order_by(VoiceProposal.created_at).limit(limit))).all():
            try:
                async with db.begin_nested():
                    if await mirror_voice_proposal(db, cipher, proposal) is not None:
                        count += 1
                    else:
                        _UNMIRRORABLE.add(proposal.id)
            except AppError as exc:
                _UNMIRRORABLE.add(proposal.id)
                log.info("Voice proposal %s not mirrored (%s)", proposal.id, exc.code)
        await db.commit()
    return count


def _spec_from_record(proposal: VoiceProposal) -> ActionSpec:
    """Rebuild the exact ActionSpec the proposal was bound to, from its stored
    canonical form. The hash must equal ``proposal.action_hash``."""
    action = proposal.action or {}
    params = action.get("params") or {}
    amount = action.get("amount")
    return ActionSpec(
        action.get("type") or proposal.action_type,
        uuid.UUID(str(action.get("scope_id"))) if action.get("scope_id") else proposal.scope_id,
        dict(params),
        amount=Decimal(str(amount)) if amount is not None else None,
        currency=action.get("currency"),
        subject=action.get("subject") if action.get("subject") is not None else proposal.subject,
    )


async def _execution_key(db, proposal_id: uuid.UUID) -> str:
    """One execution per proposal; after a failed attempt a new attempt gets a
    new key. (A failed attempt spent the approval, so a new key only ever runs
    after a fresh approval — see the module docstring.)"""
    base = f"voice:{proposal_id}"
    failed = await db.scalar(select(func.count()).select_from(PlatformActionExecution).where(
        PlatformActionExecution.idempotency_key.like(base + "%"),
        PlatformActionExecution.status == "failed"))
    return base if not failed else f"{base}:retry{failed}"


async def execute_voice_proposal(db, actor: User, ctx: AuditContext, proposal: VoiceProposal) -> VoiceProposal:
    """Consume the platform approval and dispatch the voice effect through
    ``execute_once`` with read-back verification of the proposal row.

    Raises before dispatch when the approval is missing, not approved, decided
    by the requester themself, or expired. A dispatch failure marks the
    proposal ``failed`` with the reason and spends the approval (single use).
    """
    approval = None
    if proposal.approval_id is not None:
        approval = await db.get(PlatformApprovalRequest, proposal.approval_id)
    if approval is None or approval.status not in {"approved", "consumed"}:
        raise ConflictError("This voice action is not approved.")
    if approval.decided_by is None or approval.decided_by == proposal.requested_by:
        raise PermissionDeniedError("Separation of duties: you cannot approve your own voice request.")
    if datetime.now(UTC) >= proposal.expires_at:
        raise ConflictError("This voice proposal expired before it was used. Record a new voice note.")
    if _spec_from_record(proposal).hash() != proposal.action_hash:
        raise ConflictError("The stored voice action does not match its approval hash.")
    proposal_id = proposal.id
    audit_ctx = AuditContext(actor.id, actor.email, None, None)

    async def dispatch() -> str:
        row = await db.get(VoiceProposal, proposal_id, populate_existing=True)
        if row is None:
            raise InvalidInputError("Voice proposal is gone.")
        if row.status == "executed" and row.execution_result is not None:
            return f"voice:{proposal_id}"  # already executed: replay, never act twice
        try:
            ref, result = await apply_voice_effect(db, row)
        except AppError as exc:
            row.status = "failed"
            row.failure_reason = f"{exc.code}: {exc}"[:500]
            row.updated_at = datetime.now(UTC)
            await db.flush()
            raise
        row.execution_result = result
        row.status = "executed"
        row.executed_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        await db.flush()
        record(db, audit_ctx, "voice.action_executed", target=str(proposal_id),
               details={"action_type": row.action_type, "action_hash": row.action_hash,
                        "subject": row.subject, "ref": ref})
        return ref

    async def verify(ref: str) -> VerifiedResult:
        row = await db.scalar(select(VoiceProposal).where(VoiceProposal.id == proposal_id)
                              .execution_options(populate_existing=True))
        ok = row is not None and row.status == "executed" and row.execution_result is not None
        return VerifiedResult(ok, {"proposal_id": str(proposal_id),
                                   "status": row.status if row else None, "ref": ref})

    spec = _spec_from_record(proposal)
    result = await execute_once(db, principal_for(actor, channel="workflow"),
                                await _execution_key(db, proposal_id), spec, duty=DUTY,
                                dispatch=dispatch, verify=verify, approval_id=proposal.approval_id)
    if result.status == "failed":
        raise ConflictError("The voice action could not be executed. Nothing was changed; see the audit log.")
    if result.status == "claimed":
        raise ConflictError("This voice action is being executed right now.")
    return await db.get(VoiceProposal, proposal_id, populate_existing=True)


async def _proposal(ctx: ActionContext) -> VoiceProposal | None:
    try:
        proposal_id = uuid.UUID(str(ctx.values.get("proposal_id")))
    except (ValueError, TypeError):
        return None
    row = await ctx.db.scalar(select(VoiceProposal).where(VoiceProposal.id == proposal_id)
                              .execution_options(populate_existing=True))
    return row if row is not None and row.group_id == ctx.group_id else None  # department binding


class VoiceProposalApprovalHandler:
    name = "voice.proposal_approval"
    label = "Independent approval of a voice-proposed action"
    module = "voice"
    permission = None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await _proposal(ctx)
        if row is None:
            return ActionOutcome("failed", failure_code="proposal_missing")
        out = {"proposal_id": str(row.id), "status": row.status}
        if row.status == "executed":
            return ActionOutcome("done", out)
        if row.status == "failed":
            return ActionOutcome("failed", out, "proposal_failed")
        if row.approval_id is None:
            return ActionOutcome("failed", out, "approval_missing")
        approval = await ctx.db.get(PlatformApprovalRequest, row.approval_id, populate_existing=True)
        if approval is None:
            return ActionOutcome("failed", out, "approval_missing")
        if approval.status in {"approved", "consumed"}:
            return ActionOutcome("done", out | {"approver_id": str(approval.decided_by)})
        if approval.status == "rejected":
            return ActionOutcome("failed", out, "approval_rejected")
        if (approval.status == "expired" or approval.expires_at <= datetime.now(UTC)
                or row.expires_at <= datetime.now(UTC)):
            return ActionOutcome("failed", out, "approval_expired")
        return ActionOutcome("waiting", out, poll_seconds=15)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _proposal(ctx)
        approval = None
        if row is not None and row.approval_id is not None:
            approval = await ctx.db.get(PlatformApprovalRequest, row.approval_id, populate_existing=True)
        ok = (row is not None and approval is not None and approval.status in {"approved", "consumed"}
              and approval.action_hash == row.action_hash and approval.decided_by is not None
              and approval.decided_by != row.requested_by)
        return VerifiedResult(ok, {"proposal_id": output.get("proposal_id"),
                                   "status": approval.status if approval else None})


class VoiceProposalExecuteHandler:
    name = "voice.proposal_execute"
    label = "Execute an approved voice action (execute_once, read-back)"
    module = "voice"
    permission = DUTY

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await _proposal(ctx)
        if row is None:
            return ActionOutcome("failed", failure_code="proposal_missing")
        out = {"proposal_id": str(row.id)}
        if row.status == "executed":
            return ActionOutcome("done", out | {"status": "executed"})
        if row.status == "failed":
            # A failed attempt spent the approval; a new voice note is needed.
            return ActionOutcome("failed", out | {"status": "failed"}, "proposal_failed")
        actor = await ctx.db.get(User, ctx.principal.user_id)
        if actor is None:
            return ActionOutcome("failed", out, "actor_missing")
        try:
            await execute_voice_proposal(ctx.db, actor, AuditContext(actor.id, actor.email, None, None), row)
        except PermissionDeniedError:
            await ctx.db.rollback()
            return ActionOutcome("failed", out, "actor_not_permitted")  # hand the step to someone allowed
        except AppError as exc:
            await ctx.db.rollback()
            return ActionOutcome("failed", out, exc.code[:60])
        return ActionOutcome("done", out | {"status": "executed"})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _proposal(ctx)
        ok = row is not None and row.status == "executed" and row.execution_result is not None
        return VerifiedResult(ok, {"proposal_id": output.get("proposal_id"),
                                   "status": row.status if row else None})


register(VoiceProposalApprovalHandler())
register(VoiceProposalExecuteHandler())
