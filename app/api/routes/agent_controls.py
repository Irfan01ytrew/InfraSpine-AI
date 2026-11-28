"""Step AK (slice 2): HTTP API for per-agent control records.

Every change needs `agents.manage` on the organisation scope (checked through
`app/platform`); reads need `agents.read` or `agents.manage`. An installation
administrator must be granted one of those permissions on a platform role
first — the legacy admin role alone does not imply them, by design (deny by
default). Money is rendered as decimal strings in JSON, never floats.

Kill is terminal: `POST /{agent_id}/kill` stops the agent everywhere, cancels
its queued Studio work, and writes the audit event. A killed agent comes back
only when an administrator creates a new control record for it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent_controls.evaluator import spend_status
from app.agent_controls.store import (
    create_control,
    get_control_record,
    kill_control,
    list_control_records,
    record_spend_once,
    record_to_control,
    spend_total_for,
    update_control,
)
from app.api.deps import CurrentUser, DbSession, get_current_user
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import PERMISSION_AGENTS_MANAGE, PERMISSION_AGENTS_READ
from app.db.agent_controls_models import AgentControlEvent, AgentControlRecord, AgentSpendLedger
from app.platform import access as platform_access

router = APIRouter(prefix="/api/agents/controls", tags=["agent-controls"],
                   dependencies=[Depends(get_current_user)])


class ControlCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    agent_kind: str = "studio"
    owner_id: uuid.UUID | None = None
    scope_id: uuid.UUID | None = None
    data_scopes: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    spend_limit_usd: Decimal | None = None
    alert_threshold: Decimal = Decimal("0.80")
    max_steps: int = 6
    delegate_to: list[uuid.UUID] = Field(default_factory=list)
    max_delegation_depth: int = 1


class ControlUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    status: str | None = None
    data_scopes: list[str] | None = None
    permissions: list[str] | None = None
    allowed_actions: list[str] | None = None
    spend_limit_usd: Decimal | None = None
    alert_threshold: Decimal | None = None
    max_steps: int | None = None
    delegate_to: list[uuid.UUID] | None = None
    max_delegation_depth: int | None = None
    scope_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None


class KillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class SpendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = ""
    model: str = ""
    tokens: int = Field(ge=0, default=0)
    cost_usd: Decimal = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


def _money(value: Decimal | None) -> str | None:
    """Money renders at the storage scale (4dp), so a value held in memory
    (`Decimal("25.00")`) and the same value read back from `Numeric(19, 4)`
    render identically regardless of session expiry behaviour."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.0001")))


def _out(row: AgentControlRecord) -> dict:
    control = record_to_control(row)
    state = spend_status(control)
    return {
        "agent_id": str(row.agent_id),
        "agent_kind": row.agent_kind,
        "owner_id": str(row.owner_id) if row.owner_id else None,
        "scope_id": str(row.scope_id) if row.scope_id else None,
        "status": row.status,
        "data_scopes": list(row.data_scopes or []),
        "permissions": list(row.permissions or []),
        "allowed_actions": list(row.allowed_actions or []),
        "spend_limit_usd": _money(row.spend_limit_usd),
        "spend_used_usd": _money(row.spend_used_usd),
        "spend_state": state["state"],
        "alert_threshold": _money(row.alert_threshold),
        "max_steps": row.max_steps,
        "delegate_to": [str(d) for d in (row.delegate_to or [])],
        "max_delegation_depth": row.max_delegation_depth,
        "killed_at": row.killed_at.isoformat() if row.killed_at else None,
        "killed_by": str(row.killed_by) if row.killed_by else None,
        "kill_reason": row.kill_reason,
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _require_read(db: DbSession, user: CurrentUser) -> uuid.UUID:
    """`agents.read` or `agents.manage` on the organisation scope, through the
    platform's own membership rules (non-members get 404, members without the
    permission get 403)."""
    p = platform_access.principal_for(user)
    org = await platform_access.ensure_org_scope(db)
    for permission in (PERMISSION_AGENTS_READ, PERMISSION_AGENTS_MANAGE):
        decision = await platform_access.authorize(db, p, permission, org.id)
        if decision.allowed:
            return org.id
        if decision.reason in {"scope_missing", "not_a_member"}:
            raise NotFoundError("Not found.")
    raise PermissionDeniedError("You do not have permission to view agent controls.")


async def _require_manage(db: DbSession, user: CurrentUser, scope_id: uuid.UUID | None) -> None:
    p = platform_access.principal_for(user)
    scope = scope_id or (await platform_access.ensure_org_scope(db)).id
    await platform_access.require(db, p, PERMISSION_AGENTS_MANAGE, scope)


@router.post("", status_code=201)
async def create_agent_control(body: ControlCreateIn, db: DbSession, user: CurrentUser) -> dict:
    await _require_manage(db, user, body.scope_id)
    row = await create_control(
        db, platform_access.principal_for(user), agent_id=body.agent_id, agent_kind=body.agent_kind,
        owner_id=body.owner_id, scope_id=body.scope_id, data_scopes=frozenset(body.data_scopes),
        permissions=frozenset(body.permissions), allowed_actions=frozenset(body.allowed_actions),
        spend_limit_usd=body.spend_limit_usd, alert_threshold=body.alert_threshold,
        max_steps=body.max_steps, delegate_to=frozenset(body.delegate_to),
        max_delegation_depth=body.max_delegation_depth)
    await db.commit()
    return _out(row)


@router.get("")
async def list_agent_controls(db: DbSession, user: CurrentUser, status: str | None = None,
                              scope_id: uuid.UUID | None = None) -> dict:
    await _require_read(db, user)
    rows = await list_control_records(db, status=status, scope_id=scope_id)
    return {"controls": [_out(row) for row in rows]}


@router.get("/{agent_id}")
async def get_agent_control(agent_id: uuid.UUID, db: DbSession, user: CurrentUser) -> dict:
    await _require_read(db, user)
    row = await get_control_record(db, agent_id)
    if row is None:
        raise NotFoundError("Agent control record not found.")
    return _out(row)


@router.patch("/{agent_id}")
async def patch_agent_control(agent_id: uuid.UUID, body: ControlUpdateIn, db: DbSession,
                              user: CurrentUser) -> dict:
    await _require_manage(db, user, body.scope_id)
    changes = {key: value for key, value in body.model_dump(exclude={"reason"}, exclude_unset=True).items()
               if value is not None or key == "spend_limit_usd"}
    if not changes:
        raise InvalidInputError("Nothing to change.")
    row = await update_control(db, platform_access.principal_for(user), agent_id=agent_id,
                               reason=body.reason, **changes)
    await db.commit()
    return _out(row)


@router.post("/{agent_id}/kill")
async def kill_agent_control(agent_id: uuid.UUID, body: KillIn, db: DbSession,
                             user: CurrentUser) -> dict:
    row = await get_control_record(db, agent_id)
    if row is None:
        raise NotFoundError("Agent control record not found.")
    await _require_manage(db, user, row.scope_id)
    row, counts = await kill_control(db, platform_access.principal_for(user), agent_id=agent_id,
                                     reason=body.reason)
    await db.commit()
    return {**_out(row), "cancelled_work": counts}


@router.get("/{agent_id}/events")
async def list_agent_control_events(agent_id: uuid.UUID, db: DbSession, user: CurrentUser,
                                    limit: int = 50) -> dict:
    await _require_read(db, user)
    if await get_control_record(db, agent_id) is None:
        raise NotFoundError("Agent control record not found.")
    rows = await db.scalars(select(AgentControlEvent).where(AgentControlEvent.agent_id == agent_id)
                            .order_by(AgentControlEvent.created_at.desc(), AgentControlEvent.id.desc())
                            .limit(max(1, min(limit, 200))))
    return {"events": [{
        "id": row.id, "event": row.event, "actor_id": str(row.actor_id) if row.actor_id else None,
        "reason": row.reason, "before": row.before, "after": row.after,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]}


@router.post("/{agent_id}/spend")
async def record_agent_spend(agent_id: uuid.UUID, body: SpendIn, db: DbSession,
                             user: CurrentUser) -> dict:
    row = await get_control_record(db, agent_id)
    if row is None:
        raise NotFoundError("Agent control record not found.")
    await _require_manage(db, user, row.scope_id)
    result = await record_spend_once(
        db, platform_access.principal_for(user), agent_id=agent_id, provider=body.provider,
        model=body.model, tokens=body.tokens, cost_usd=body.cost_usd,
        idempotency_key=body.idempotency_key)
    await db.commit()
    used = await spend_total_for(db, agent_id)
    return {"agent_id": str(agent_id), "execution_status": result.status,
            "replayed": result.replayed, "spend_used_usd": str(used),
            "ledger_id": result.external_ref}


@router.get("/{agent_id}/spend")
async def get_agent_spend(agent_id: uuid.UUID, db: DbSession, user: CurrentUser) -> dict:
    await _require_read(db, user)
    row = await get_control_record(db, agent_id)
    if row is None:
        raise NotFoundError("Agent control record not found.")
    state = spend_status(record_to_control(row))
    rows = await db.scalars(select(AgentSpendLedger).where(AgentSpendLedger.agent_id == agent_id)
                            .order_by(AgentSpendLedger.recorded_at.desc()).limit(20))
    return {
        "agent_id": str(agent_id),
        "spend_used_usd": str(row.spend_used_usd),
        "spend_limit_usd": _money(row.spend_limit_usd),
        "spend_state": state["state"],
        "recent": [{
            "id": str(r.id), "provider": r.provider, "model": r.model, "tokens": r.tokens,
            "cost_usd": str(r.cost_usd), "idempotency_key": r.idempotency_key,
            "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
        } for r in rows],
    }
