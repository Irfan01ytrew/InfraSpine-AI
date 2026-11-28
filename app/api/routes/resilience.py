"""Slice 7: resilience read API.

Read-only except for the audit export (owner-only). The overview is the
caller's authorized projection of the department's dependency map: ranked
risks, recovery plans, recent audit events and freshness. The simulate
endpoint scores one disruption over the same snapshot (advisory only).
"""

from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.platform.access import authorize, principal_for
from app.resilience.audit import build_audit_export, export_csv, resilience_audit_trail
from app.resilience.impact import DISRUPTION_KINDS
from app.resilience.service import (
    overview_for_scope,
    plan_progress_for_scope,
    simulate_for_scope,
)
from app.workflows.action_runtime import platform_scope_for

router = APIRouter(prefix="/api/resilience", tags=["resilience"])


class SimulateIn(BaseModel):
    kind: str
    target_ids: list[str] = Field(min_length=1, max_length=50)
    duration_days: int = Field(default=1, ge=1, le=365)


@router.get("/scopes/{scope_id}/overview")
async def overview(scope_id: UUID, db: DbSession, user: CurrentUser):
    return await overview_for_scope(db, user, scope_id)


@router.post("/scopes/{scope_id}/simulate")
async def simulate(scope_id: UUID, body: SimulateIn, db: DbSession, user: CurrentUser):
    if body.kind not in DISRUPTION_KINDS:
        raise InvalidInputError(f"Unknown disruption kind: {body.kind!r}.")
    try:
        return await simulate_for_scope(
            db, user, scope_id,
            {"kind": body.kind, "target_ids": body.target_ids, "duration_days": body.duration_days},
        )
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


@router.get("/scopes/{scope_id}/plans/{plan_id}")
async def plan_progress_route(scope_id: UUID, plan_id: str, db: DbSession, user: CurrentUser):
    return await plan_progress_for_scope(db, user, scope_id, plan_id)


@router.get("/scopes/{scope_id}/audit/export")
async def audit_export(
    scope_id: UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    format: str = Query(default="csv", pattern="^(csv|json)$"),
):
    """Owner-only audit export: the trail is the owner's record of record."""
    await _require_owner(db, user, scope_id)
    rows = await resilience_audit_trail(db, scope_id=str(scope_id), limit=1000)
    export = build_audit_export(rows)
    if format == "json":
        return export
    return PlainTextResponse(
        export_csv(export),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=\"resilience-audit.csv\""},
    )


async def _require_owner(db, user, scope_id):
    """platform.manage on the scope's platform scope (owner/admins only)."""
    platform_scope_id = await platform_scope_for(db, scope_id)
    if platform_scope_id is None:
        raise NotFoundError("Workflow scope not found.")
    decision = await authorize(db, principal_for(user), "platform.manage", platform_scope_id)
    if not decision.allowed:
        raise PermissionDeniedError("Only the owner may export the resilience audit trail.")
