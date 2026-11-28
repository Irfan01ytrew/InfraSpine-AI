"""Step AK (slice 2): persistence for per-agent control records.

All changes go through here so every control change writes its audit event and
no caller can change a record without `agents.manage` on the record's scope.
An agent can never change its own record: the check runs before any write, and
the platform permission check would refuse it too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_controls.evaluator import (
    DEFAULT_ALERT_THRESHOLD,
    AgentControl,
    kill_switch,
    validate_control,
)
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_controls_models import AgentControlEvent, AgentControlRecord, AgentSpendLedger
from app.db.agent_studio_models import AgentStudioApproval, AgentStudioRun
from app.platform import access as platform_access
from app.platform.access import Principal
from app.platform.approvals import ActionSpec
from app.platform.events import audit, publish
from app.platform.execution import VerifiedResult, execute_once

__all__ = [
    "record_to_control",
    "get_control_record",
    "list_control_records",
    "create_control",
    "update_control",
    "kill_control",
    "record_spend",
    "record_spend_once",
    "spend_total_for",
]

_UPDATABLE = frozenset({
    "status", "data_scopes", "permissions", "allowed_actions", "spend_limit_usd",
    "alert_threshold", "max_steps", "delegate_to", "max_delegation_depth",
    "scope_id", "owner_id",
})


def _money(value: Any, what: str) -> Decimal:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - any unparsable value is invalid input
        raise ValueError(f"{what} must be a decimal amount.") from exc
    if amount.is_nan() or amount.is_infinite():
        raise ValueError(f"{what} must be a finite amount.")
    return amount


def _dec(value: Decimal | None) -> str | None:
    """Money in audit snapshots is always 4dp, so a value read back from the
    database (`Numeric(19, 4)` -> `Decimal("10.0000")`) and the same value
    held in memory (`Decimal("10.00")`) snapshot identically."""
    if value is None:
        return None
    return str(_money(value, "amount").quantize(Decimal("0.0001")))


def _jsonable_control(control: AgentControl) -> dict:
    """A JSON-safe snapshot of a control record for the audit trail."""
    return {
        "agent_id": str(control.agent_id),
        "agent_kind": control.agent_kind,
        "owner_id": str(control.owner_id) if control.owner_id else None,
        "status": control.status,
        "data_scopes": sorted(control.data_scopes),
        "permissions": sorted(control.permissions),
        "allowed_actions": sorted(control.allowed_actions),
        "spend_limit_usd": _dec(control.spend_limit_usd),
        "spend_used_usd": _dec(control.spend_used_usd),
        "alert_threshold": _dec(control.alert_threshold),
        "max_steps": control.max_steps,
        "delegate_to": sorted(str(d) for d in control.delegate_to),
        "max_delegation_depth": control.max_delegation_depth,
        "killed_at": control.killed_at.isoformat() if control.killed_at else None,
        "killed_by": str(control.killed_by) if control.killed_by else None,
        "kill_reason": control.kill_reason,
    }


def record_to_control(row: AgentControlRecord) -> AgentControl:
    """The evaluator's view of a stored record. Money is Decimal, never float."""
    return AgentControl(
        agent_id=row.agent_id,
        agent_kind=row.agent_kind,
        owner_id=row.owner_id,
        status=row.status,
        data_scopes=frozenset(row.data_scopes or []),
        permissions=frozenset(row.permissions or []),
        allowed_actions=frozenset(row.allowed_actions or []),
        spend_limit_usd=_money(row.spend_limit_usd, "spend_limit_usd")
        if row.spend_limit_usd is not None else None,
        spend_used_usd=_money(row.spend_used_usd, "spend_used_usd"),
        alert_threshold=_money(row.alert_threshold, "alert_threshold"),
        max_steps=row.max_steps,
        delegate_to=frozenset(uuid.UUID(str(d)) for d in (row.delegate_to or [])),
        max_delegation_depth=row.max_delegation_depth,
        killed_at=row.killed_at,
        killed_by=row.killed_by,
        kill_reason=row.kill_reason,
    )


def _control_to_columns(control: AgentControl) -> dict:
    return {
        "agent_kind": control.agent_kind,
        "owner_id": control.owner_id,
        "status": control.status,
        "data_scopes": sorted(control.data_scopes),
        "permissions": sorted(control.permissions),
        "allowed_actions": sorted(control.allowed_actions),
        "spend_limit_usd": control.spend_limit_usd,
        "spend_used_usd": control.spend_used_usd,
        "alert_threshold": control.alert_threshold,
        "max_steps": control.max_steps,
        "delegate_to": sorted(str(d) for d in control.delegate_to),
        "max_delegation_depth": control.max_delegation_depth,
        "killed_at": control.killed_at,
        "killed_by": control.killed_by,
        "kill_reason": control.kill_reason,
    }


async def _require_manage(db: AsyncSession, actor: Principal, scope_id: uuid.UUID | None) -> uuid.UUID:
    """The scope a control change is authorised on (the record's scope, else org)."""
    scope = scope_id
    if scope is None:
        scope = (await platform_access.ensure_org_scope(db)).id
    await platform_access.require(db, actor, "agents.manage", scope)
    return scope


def _refuse_self_change(actor: Principal, agent_id: uuid.UUID) -> None:
    if actor.kind == "agent" and actor.agent_id == agent_id:
        raise PermissionDeniedError("An agent cannot change its own controls.")


def _jsonable(value: Any) -> Any:
    """Make an audit snapshot JSON-safe: datetimes to ISO strings, Decimals
    and UUIDs to strings, mappings/sequences recursed. JSONB columns reject
    anything else (a raw datetime from the evaluator's kill event broke the
    first test run)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_jsonable(item) for item in value]
    return value


async def _append_event(db: AsyncSession, *, agent_id: uuid.UUID, event: str, actor: Principal | None,
                        reason: str, before: dict, after: dict) -> None:
    db.add(AgentControlEvent(agent_id=agent_id, event=event,
                             actor_id=actor.user_id if actor is not None else None,
                             reason=" ".join(str(reason or "").split())[:300] or "unspecified",
                             before=_jsonable(before), after=_jsonable(after)))


async def get_control_record(db: AsyncSession, agent_id: uuid.UUID) -> AgentControlRecord | None:
    return await db.scalar(
        select(AgentControlRecord).where(AgentControlRecord.agent_id == agent_id))


async def list_control_records(db: AsyncSession, *, status: str | None = None,
                               scope_id: uuid.UUID | None = None) -> list[AgentControlRecord]:
    query = select(AgentControlRecord).order_by(AgentControlRecord.created_at, AgentControlRecord.agent_id)
    if status is not None:
        query = query.where(AgentControlRecord.status == status)
    if scope_id is not None:
        query = query.where(AgentControlRecord.scope_id == scope_id)
    return list(await db.scalars(query))


async def create_control(db: AsyncSession, actor: Principal, *, agent_id: uuid.UUID,
                         agent_kind: str = "studio", owner_id: uuid.UUID | None = None,
                         scope_id: uuid.UUID | None = None,
                         data_scopes: frozenset[str] = frozenset(),
                         permissions: frozenset[str] = frozenset(),
                         allowed_actions: frozenset[str] = frozenset(),
                         spend_limit_usd: Decimal | None = None,
                         alert_threshold: Decimal = DEFAULT_ALERT_THRESHOLD,
                         max_steps: int = 6, delegate_to: frozenset[uuid.UUID] = frozenset(),
                         max_delegation_depth: int = 1) -> AgentControlRecord:
    """Create a control record. The creator needs `agents.manage`; an agent can
    never create (or widen) its own record."""
    _refuse_self_change(actor, agent_id)
    scope = await _require_manage(db, actor, scope_id)
    control = validate_control(AgentControl(
        agent_id=agent_id, agent_kind=agent_kind, owner_id=owner_id, status="active",
        data_scopes=data_scopes, permissions=permissions, allowed_actions=allowed_actions,
        spend_limit_usd=spend_limit_usd, spend_used_usd=Decimal("0"), alert_threshold=alert_threshold,
        max_steps=max_steps, delegate_to=delegate_to, max_delegation_depth=max_delegation_depth))
    if await get_control_record(db, agent_id) is not None:
        raise ConflictError("This agent already has a control record.")
    row = AgentControlRecord(agent_id=agent_id, scope_id=scope_id, **_control_to_columns(control))
    db.add(row)
    await db.flush()
    await _append_event(db, agent_id=agent_id, event="agent.control_created", actor=actor,
                        reason="control record created", before={}, after=_jsonable_control(control))
    audit(db, actor, "agent.control_created", target=str(agent_id),
          details={"agent_kind": agent_kind, "scope_id": str(scope)})
    return row


async def update_control(db: AsyncSession, actor: Principal, *, agent_id: uuid.UUID, reason: str,
                         **changes: Any) -> AgentControlRecord:
    """Change a control record. Every change bumps `version` and writes an audit
    event with before/after values. Killing goes through `kill_control`, never here."""
    _refuse_self_change(actor, agent_id)
    unknown = set(changes) - _UPDATABLE
    if unknown:
        raise InvalidInputError(f"Cannot change: {', '.join(sorted(unknown))}.")
    if changes.get("status") == "killed":
        raise InvalidInputError("Kill an agent through kill_control, not by editing its status.")
    row = await db.scalar(select(AgentControlRecord).where(AgentControlRecord.agent_id == agent_id)
                          .with_for_update())
    if row is None:
        raise NotFoundError("Agent control record not found.")
    await _require_manage(db, actor, row.scope_id)
    before = record_to_control(row)
    merged = {
        "agent_kind": before.agent_kind, "owner_id": before.owner_id, "status": before.status,
        "data_scopes": before.data_scopes, "permissions": before.permissions,
        "allowed_actions": before.allowed_actions, "spend_limit_usd": before.spend_limit_usd,
        "spend_used_usd": before.spend_used_usd, "alert_threshold": before.alert_threshold,
        "max_steps": before.max_steps, "delegate_to": before.delegate_to,
        "max_delegation_depth": before.max_delegation_depth,
        "killed_at": before.killed_at, "killed_by": before.killed_by, "kill_reason": before.kill_reason,
    }
    for key, value in changes.items():
        if key in {"data_scopes", "permissions", "allowed_actions"}:
            merged[key] = frozenset(value or [])
        elif key == "delegate_to":
            merged[key] = frozenset(uuid.UUID(str(d)) for d in (value or []))
        elif key in {"spend_limit_usd"}:
            merged[key] = _money(value, key) if value is not None else None
        elif key == "alert_threshold":
            merged[key] = _money(value, key)
        else:
            merged[key] = value
    after = validate_control(AgentControl(agent_id=agent_id, **merged))
    for column, value in _control_to_columns(after).items():
        setattr(row, column, value)
    row.version = row.version + 1
    row.updated_at = datetime.now(UTC)
    await _append_event(db, agent_id=agent_id, event="agent.control_updated", actor=actor,
                        reason=reason, before=_jsonable_control(before), after=_jsonable_control(after))
    audit(db, actor, "agent.control_updated", target=str(agent_id),
          details={"reason": " ".join(str(reason or "").split())[:300] or "unspecified",
                   "changed": sorted(changes)})
    return row


async def _cancel_agent_work(db: AsyncSession, agent_id: uuid.UUID) -> dict[str, int]:
    """Stop work queued for a killed agent: its Studio runs and their pending
    tool approvals. Work already dispatched is left to its own reconciliation,
    never blindly undone (same rule as `cancel_queued_work`)."""
    now = datetime.now(UTC)
    runs = await db.execute(update(AgentStudioRun).where(
        AgentStudioRun.agent_id == agent_id,
        AgentStudioRun.status.in_(("queued", "awaiting_approval")),
    ).values(status="cancelled", failure_code="agent_killed", finished_at=now))
    approvals = await db.execute(update(AgentStudioApproval).where(
        AgentStudioApproval.run_id.in_(
            select(AgentStudioRun.id).where(AgentStudioRun.agent_id == agent_id)),
        AgentStudioApproval.status == "pending",
    ).values(status="rejected", decision_note="Agent killed", decided_at=now))
    return {"agent_runs": runs.rowcount or 0, "agent_approvals": approvals.rowcount or 0}


async def kill_control(db: AsyncSession, actor: Principal, *, agent_id: uuid.UUID,
                       reason: str) -> tuple[AgentControlRecord, dict[str, int]]:
    """The terminal kill-switch. The killed record stays killed until an
    administrator deliberately creates a new record; the kill event stays in
    the audit trail forever."""
    _refuse_self_change(actor, agent_id)
    row = await db.scalar(select(AgentControlRecord).where(AgentControlRecord.agent_id == agent_id)
                          .with_for_update())
    if row is None:
        raise NotFoundError("Agent control record not found.")
    await _require_manage(db, actor, row.scope_id)
    before = record_to_control(row)
    killed, event = kill_switch(before, actor_id=actor.user_id, reason=reason)
    for column, value in _control_to_columns(killed).items():
        setattr(row, column, value)
    row.version = row.version + 1
    row.updated_at = datetime.now(UTC)
    counts = await _cancel_agent_work(db, agent_id)
    await _append_event(db, agent_id=agent_id, event=event.event, actor=actor, reason=event.reason,
                        before=event.before, after={**event.after, **counts})
    audit(db, actor, "agent.killed", target=str(agent_id),
          details={"reason": event.reason, **counts})
    await publish(db, "agent.killed", row.scope_id,
                  {"agent_id": str(agent_id), "reason": event.reason, **counts}, actor.trace_id)
    return row, counts


async def spend_total_for(db: AsyncSession, agent_id: uuid.UUID) -> Decimal:
    """The ledger sum for an agent — the only trusted source of `spend_used_usd`."""
    total = await db.scalar(select(func.coalesce(func.sum(AgentSpendLedger.cost_usd), 0))
                            .where(AgentSpendLedger.agent_id == agent_id))
    return _money(total, "spend total")


async def record_spend(db: AsyncSession, *, agent_id: uuid.UUID, provider: str, model: str,
                       tokens: int, cost_usd: Decimal, idempotency_key: str) -> tuple[bool, Decimal]:
    """Record one costed call, exactly once. A retry with the same key inserts
    nothing and counts nothing twice; `spend_used_usd` is always the ledger sum
    read back after the insert."""
    cost = _money(cost_usd, "cost_usd")
    if cost < 0:
        raise InvalidInputError("cost_usd cannot be negative.")
    if tokens < 0:
        raise InvalidInputError("tokens cannot be negative.")
    if not idempotency_key or len(idempotency_key) > 200:
        raise InvalidInputError("An idempotency key of 1 to 200 characters is required.")
    inserted = await db.scalar(insert(AgentSpendLedger).values(
        id=uuid.uuid4(), agent_id=agent_id, provider=(provider or "")[:64], model=(model or "")[:160],
        tokens=tokens, cost_usd=cost, idempotency_key=idempotency_key,
    ).on_conflict_do_nothing(index_elements=["idempotency_key"]).returning(AgentSpendLedger.id))
    total = await spend_total_for(db, agent_id)
    row = await get_control_record(db, agent_id)
    if row is not None and row.spend_used_usd != total:
        row.spend_used_usd = total
        row.updated_at = datetime.now(UTC)
    return inserted is not None, total


async def record_spend_once(db: AsyncSession, principal: Principal, *, agent_id: uuid.UUID,
                            provider: str, model: str, tokens: int, cost_usd: Decimal,
                            idempotency_key: str):
    """Record spend through the platform's exactly-once execution: the ledger
    row is the dispatch and the read-back of that row is the verification, so
    a crash or retry can never double-count a call. The caller needs
    `agents.manage` on the record's scope (a worker service principal)."""
    row = await get_control_record(db, agent_id)
    scope_id = row.scope_id if row is not None and row.scope_id is not None else (
        await platform_access.ensure_org_scope(db)).id
    cost = _money(cost_usd, "cost_usd")
    spec = ActionSpec("agent.spend_record", scope_id,
                      {"agent_id": str(agent_id), "provider": provider or "", "model": model or "",
                       "tokens": tokens, "cost_usd": str(cost), "idempotency_key": idempotency_key},
                      amount=cost, subject=f"agent:{agent_id}")

    async def dispatch() -> str:
        inserted, _total = await record_spend(
            db, agent_id=agent_id, provider=provider, model=model, tokens=tokens,
            cost_usd=cost, idempotency_key=idempotency_key)
        if not inserted:
            existing = await db.scalar(select(AgentSpendLedger.id).where(
                AgentSpendLedger.idempotency_key == idempotency_key))
            return str(existing)
        ledger_id = await db.scalar(select(AgentSpendLedger.id).where(
            AgentSpendLedger.idempotency_key == idempotency_key))
        return str(ledger_id)

    async def verify(ref: str) -> VerifiedResult:
        found = await db.scalar(select(AgentSpendLedger).where(
            AgentSpendLedger.idempotency_key == idempotency_key))
        ok = found is not None and str(found.id) == ref and _money(found.cost_usd, "cost_usd") == cost
        return VerifiedResult(ok, {"ledger_id": ref, "cost_usd": str(cost)})

    return await execute_once(db, principal, f"agent-spend:{idempotency_key}", spec,
                              duty="agents.manage", dispatch=dispatch, verify=verify)
