"""Step W slice 3 — remedy executor.

Runs the detector's remedy decision through the platform's exactly-once
execution (``execute_once``) with read-back verification, and records every
attempt in the audit log and on the incident row.

Safety contract (enforced here, not just documented):
- Only allow-listed remedies from ``detector.SAFE_REMEDIES`` are ever attempted;
  ``decide_action`` still gates severity, attempt budget, and cooldown.
- A remedy with no *verified* handler is never executed: the executor escalates
  instead of pretending. Today that means ``restart_worker`` (no supervisor API
  exists in the app) and ``switch_fallback`` (no configured fallback target)
  always escalate until a later slice supplies a verifiable handler.
- Shipped handlers only release expired leases — they never write business data:
  ``clear_stale_lock`` releases an expired workflow-step lease, ``requeue_job``
  returns a stuck ingestion document to the queue (mirroring the worker's own
  ``_release``). ``retry_once`` runs a caller-supplied operation exactly once.
- Every dispatch runs under the ``self_healing.remedy`` duty, so the acting
  principal must hold that permission on the scope; the legacy admin bridge
  deliberately does NOT grant it. No remedy ever needs, or takes, an approval:
  anything approval-worthy is an escalation by definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.db.models import DOC_PENDING, DOC_PROCESSING, Document
from app.db.workflow_engine_models import WorkflowStep
from app.platform import ActionSpec, Principal, VerifiedResult, execute_once
from app.platform.events import audit
from app.self_healing.collector import attempt_history, record_incident
from app.self_healing.detector import (
    MAX_ATTEMPTS,
    Incident,
    decide_action,
    scrub,
)
from app.self_healing.escalation import record_escalation
from app.self_healing.models import SelfHealingIncident

DUTY_SELF_HEALING_REMEDY = "self_healing.remedy"


@dataclass(frozen=True)
class RemedyOutcome:
    """What the executor decided and what happened."""

    action: str  # "remedy" | "escalate"
    remedy: str | None
    status: str  # execution status ("verified"/"failed"/"dispatched") or "escalated"
    execution_id: UUID | None
    replayed: bool
    reason: str


@dataclass(frozen=True)
class RemedyHandler:
    """A verifiable remedy: dispatch returns an external ref, verify reads it back."""

    dispatch: Callable[[AsyncSession, Incident], Awaitable[str]]
    verify: Callable[[AsyncSession, str], Awaitable[VerifiedResult]]


# --------------------------------------------------------------------------- handlers

async def _clear_stale_lock_dispatch(db: AsyncSession, incident: Incident) -> str:
    """Release the stalest expired workflow-step lease. Lock only, never data."""
    now = datetime.now(UTC)
    step_id = await db.scalar(
        select(WorkflowStep.id)
        .where(
            WorkflowStep.status == "running",
            WorkflowStep.lease_until.is_not(None),
            WorkflowStep.lease_until < now,
        )
        .order_by(WorkflowStep.lease_until.asc())
        .limit(1)
    )
    if step_id is None:
        raise RuntimeError("no stale workflow lock found")
    await db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.id == step_id, WorkflowStep.status == "running")
        .values(lease_until=None, claim_token=None)
    )
    await db.flush()
    return f"wf_step:{step_id}"


async def _clear_stale_lock_verify(db: AsyncSession, ref: str) -> VerifiedResult:
    step_id = UUID(ref.removeprefix("wf_step:"))
    row = await db.scalar(
        select(WorkflowStep)
        .where(WorkflowStep.id == step_id)
        .execution_options(populate_existing=True)
    )
    ok = row is not None and row.lease_until is None and row.claim_token is None
    return VerifiedResult(ok, {"ref": ref, "lease_released": bool(ok)})


async def _requeue_job_dispatch(db: AsyncSession, incident: Incident) -> str:
    """Return the oldest stuck ingestion document to the queue (worker `_release`)."""
    now = datetime.now(UTC)
    doc_id = await db.scalar(
        select(Document.id)
        .where(
            Document.status == DOC_PROCESSING,
            Document.locked_until.is_not(None),
            Document.locked_until < now,
        )
        .order_by(Document.locked_until.asc())
        .limit(1)
    )
    if doc_id is None:
        raise RuntimeError("no stuck ingestion job found")
    await db.execute(
        update(Document)
        .where(Document.id == doc_id, Document.status == DOC_PROCESSING)
        .values(
            status=DOC_PENDING,
            attempts=func.greatest(Document.attempts - 1, 0),
            locked_until=None,
            next_attempt_at=now,
        )
    )
    await db.flush()
    return f"document:{doc_id}"


async def _requeue_job_verify(db: AsyncSession, ref: str) -> VerifiedResult:
    doc_id = UUID(ref.removeprefix("document:"))
    row = await db.scalar(
        select(Document)
        .where(Document.id == doc_id)
        .execution_options(populate_existing=True)
    )
    ok = row is not None and row.status == DOC_PENDING and row.locked_until is None
    return VerifiedResult(ok, {"ref": ref, "requeued": bool(ok)})


REMEDY_HANDLERS: dict[str, RemedyHandler] = {
    "clear_stale_lock": RemedyHandler(_clear_stale_lock_dispatch, _clear_stale_lock_verify),
    "requeue_job": RemedyHandler(_requeue_job_dispatch, _requeue_job_verify),
}
# "retry_once" is handled separately: it needs a caller-supplied operation.
# "restart_worker" and "switch_fallback" have no verified handler yet (no
# supervisor API / configured fallback target exists); they always escalate.


# --------------------------------------------------------------------------- executor

async def _escalate(
    db: AsyncSession,
    principal: Principal,
    incident: Incident,
    row: SelfHealingIncident,
    reason: str,
    *,
    now: datetime,
) -> RemedyOutcome:
    row.status = "escalated"
    row.updated_at = now
    # The escalation is delivered to the owner (admin UI) and recorded, so
    # an escalated incident is never silent.
    await record_escalation(db, incident, attempt_history(row), now=now)
    audit(
        db, principal, "self_healing.escalated", outcome="success",
        target=scrub(incident.key)[:100],
        details={"kind": incident.kind, "severity": incident.severity, "reason": scrub(reason)[:500]},
    )
    await db.commit()
    return RemedyOutcome(
        action="escalate", remedy=None, status="escalated",
        execution_id=None, replayed=False, reason=reason,
    )


async def execute_remedy(
    db: AsyncSession,
    principal: Principal,
    incident: Incident,
    *,
    scope_id: UUID,
    operation: Callable[[], Awaitable[str]] | None = None,
    check: Callable[[str], Awaitable[bool]] | None = None,
    now: datetime | None = None,
) -> RemedyOutcome:
    """Decide (via the detector) and, when allowed, execute one remedy exactly once.

    ``operation``/``check`` are only used by the ``retry_once`` remedy: the
    operation runs once under the idempotency key and ``check`` verifies its ref.
    Raises ``PermissionDeniedError``/``NotFoundError`` when the principal lacks
    the ``self_healing.remedy`` duty on the scope (via ``execute_once``).
    """
    moment = now or datetime.now(UTC)
    if len(incident.key) > 160:
        raise InvalidInputError("Incident key is too long for an idempotency key.")

    row = await record_incident(db, incident, now=moment)
    await db.commit()
    history = attempt_history(row)
    decision = decide_action(incident, history, now=moment)
    if decision.action == "escalate":
        return await _escalate(db, principal, incident, row, decision.reason, now=moment)

    remedy = decision.remedy
    assert remedy is not None  # decide_action only returns remedy with a remedy set
    if remedy == "retry_once":
        if operation is None or check is None:
            return await _escalate(
                db, principal, incident, row,
                "retry_once needs a caller-supplied operation and check", now=moment,
            )

        async def _retry_dispatch(_db: AsyncSession, _incident: Incident) -> str:
            return await operation()

        async def _retry_verify(_db: AsyncSession, ref: str) -> VerifiedResult:
            return VerifiedResult(bool(await check(ref)), {"ref": ref})

        handler = RemedyHandler(_retry_dispatch, _retry_verify)
    else:
        handler = REMEDY_HANDLERS.get(remedy)
        if handler is None:
            return await _escalate(
                db, principal, incident, row,
                f"no verified handler for remedy {remedy!r}; escalation only", now=moment,
            )

    key = f"self-healing:{incident.key}:attempt-{history.attempts + 1}"
    action = ActionSpec(
        f"self_healing.{remedy}",
        scope_id,
        {"incident_key": incident.key, "kind": incident.kind, "remedy": remedy},
        subject=f"self-healing:{incident.key}",
    )

    async def _dispatch() -> str:
        return await handler.dispatch(db, incident)

    async def _verify(ref: str) -> VerifiedResult:
        return await handler.verify(db, ref)

    result = await execute_once(
        db, principal, key, action,
        duty=DUTY_SELF_HEALING_REMEDY, dispatch=_dispatch, verify=_verify,
    )

    # execute_once commits internally; bookkeeping lands in a fresh transaction.
    row = await db.scalar(select(SelfHealingIncident).where(SelfHealingIncident.key == incident.key))
    if not result.replayed:
        row.attempts += 1
        row.last_attempt_at = moment
    row.status = (
        "remedied"
        if result.status == "verified"
        else ("escalated" if row.attempts >= MAX_ATTEMPTS else "open")
    )
    if row.status == "escalated":
        # Auto-remedy budget exhausted: the incident escalates to the owner
        # and the delivery is recorded, like any other escalation.
        await record_escalation(db, incident, attempt_history(row), now=moment)
    row.updated_at = moment
    audit(
        db, principal, "self_healing.remedy_decision", outcome="success",
        target=scrub(incident.key)[:100],
        details={
            "kind": incident.kind,
            "remedy": remedy,
            "status": result.status,
            "replayed": result.replayed,
            "attempts": row.attempts,
        },
    )
    await db.commit()
    return RemedyOutcome(
        action="remedy", remedy=remedy, status=result.status,
        execution_id=result.execution_id, replayed=result.replayed,
        reason=decision.reason,
    )
