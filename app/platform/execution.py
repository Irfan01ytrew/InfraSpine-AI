"""Exactly-once execution with read-back verification.

`execute_once` writes a ledger row keyed by the idempotency key before anything is dispatched. Only the caller that
created the row dispatches; every other caller (a retry, a duplicate webhook, a concurrent worker) gets the stored
result. "Done" means verified: the target system is read back after dispatch. A crash after dispatch is resumed by
verifying again, never by dispatching again.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.platform.access import Principal, authorize
from app.platform.approvals import ActionSpec, assert_separation_of_duties, consume_approval
from app.platform.events import audit, publish
from app.platform.models import PlatformActionExecution


@dataclass(frozen=True)
class VerifiedResult:
    ok: bool
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: uuid.UUID
    status: str  # claimed (in progress elsewhere) | dispatched | verified | failed
    external_ref: str | None
    verified_result: dict | None
    replayed: bool


def _result(row: PlatformActionExecution, replayed: bool) -> ExecutionResult:
    return ExecutionResult(row.id, row.status, row.external_ref, row.verified_result, replayed)


async def execute_once(
    db: AsyncSession, p: Principal, key: str, action: ActionSpec, *, duty: str,
    dispatch: Callable[[], Awaitable[str]], verify: Callable[[str], Awaitable[VerifiedResult]],
    approval_id: uuid.UUID | None = None,
) -> ExecutionResult:
    if not key or len(key) > 200:
        raise InvalidInputError("An idempotency key of 1 to 200 characters is required.")
    existing = await db.scalar(select(PlatformActionExecution).where(PlatformActionExecution.idempotency_key == key))
    if existing is not None:
        return await _replay(db, p, existing, action, verify)

    decision = await authorize(db, p, duty, action.scope_id, amount=action.amount)
    if not decision.allowed:
        if decision.reason in {"scope_missing", "not_a_member"}:
            raise NotFoundError("Not found.")
        raise PermissionDeniedError("You do not have permission for this action.")
    if decision.requires_approval and approval_id is None:
        raise PermissionDeniedError(f"This action needs {decision.approval_permission} approval first.")
    await assert_separation_of_duties(db, p.user_id, action.subject, duty)

    execution_id = uuid.uuid4()
    claimed = await db.scalar(insert(PlatformActionExecution).values(
        id=execution_id, idempotency_key=key, action_type=action.type, action_hash=action.hash(), duty=duty,
        subject=action.subject, scope_id=action.scope_id, approval_id=approval_id, executor_user_id=p.user_id,
        executor_kind=p.kind, status="claimed", trace_id=p.trace_id,
    ).on_conflict_do_nothing(index_elements=["idempotency_key"]).returning(PlatformActionExecution.id))
    if claimed is None:  # a concurrent caller created the row first (we waited on its commit)
        await db.rollback()
        existing = await db.scalar(select(PlatformActionExecution).where(PlatformActionExecution.idempotency_key == key))
        return await _replay(db, p, existing, action, verify)
    if approval_id is not None:
        grant = await consume_approval(db, approval_id, action)  # raises and rolls back the claim on mismatch
        if decision.approval_permission and grant.required_permission != decision.approval_permission:
            await db.rollback()
            raise PermissionDeniedError(f"This action needs {decision.approval_permission} approval.")
    await db.commit()

    row = await db.get(PlatformActionExecution, execution_id)
    try:
        ref = str(await dispatch())
    except Exception as exc:
        # A dispatch database error aborts the transaction: roll back
        # first, or the failure-marking writes below can never commit and
        # the execution stays 'claimed' forever (a replay then waits
        # forever instead of failing).
        await db.rollback()
        row = await db.get(PlatformActionExecution, execution_id)
        row.status, row.failure_code = "failed", f"dispatch_{type(exc).__name__}"[:80]
        await publish(db, "action.failed", action.scope_id, {"execution_id": str(row.id), "stage": "dispatch"}, p.trace_id)
        audit(db, p, "platform.action_failed", outcome="failure", target=str(row.id),
              details={"action_type": action.type, "stage": "dispatch"})
        await db.commit()
        return _result(row, False)
    row.status, row.external_ref, row.dispatched_at = "dispatched", ref[:500], datetime.now(UTC)
    await db.commit()
    return await _verify(db, p, row, verify, replayed=False)


async def _verify(db, p, row, verify, *, replayed: bool) -> ExecutionResult:
    try:
        checked = await verify(row.external_ref)
    except Exception as exc:  # read-back unavailable: stays dispatched; a retry verifies again
        row.failure_code = f"verify_{type(exc).__name__}"[:80]
        await db.commit()
        return _result(row, replayed)
    row.verified_result = checked.details
    row.verified_at = datetime.now(UTC)
    row.status = "verified" if checked.ok else "failed"
    row.failure_code = None if checked.ok else "verification_failed"
    await publish(db, "action.verified" if checked.ok else "action.failed", row.scope_id,
                  {"execution_id": str(row.id), "external_ref": row.external_ref}, p.trace_id)
    audit(db, p, "platform.action_verified" if checked.ok else "platform.action_failed",
          outcome="success" if checked.ok else "failure", target=str(row.id),
          details={"action_type": row.action_type, "external_ref": row.external_ref})
    await db.commit()
    return _result(row, replayed)


async def _replay(db, p, row, action: ActionSpec, verify) -> ExecutionResult:
    if row.action_hash != action.hash():
        raise ConflictError("This idempotency key was already used for a different action.")
    if row.status == "dispatched":
        row_id = row.id
        locked = await db.scalar(select(PlatformActionExecution).where(PlatformActionExecution.id == row_id)
                                 .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if locked is not None and locked.status == "dispatched":
            return await _verify(db, p, locked, verify, replayed=True)
        await db.rollback()
        row = await db.get(PlatformActionExecution, row_id, populate_existing=True)
    return _result(row, True)
