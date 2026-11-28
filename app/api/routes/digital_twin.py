"""Step S (slices 5–6): read-only digital-twin API.

Tenant-addressed, like the company-graph API: the caller must be a
member of the graph tenant (``scope_for`` → 404 for unknown tenant or
non-member). The twin is a projection of authorized data — these
endpoints only ever read. No twin endpoint approves, dispatches,
grants access, or pays anything.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.company_graph.access import scope_for
from app.core.errors import InvalidInputError
from app.digital_twin.service import predict_for_scope, run_whatif_for_scope, twin_overview
from app.digital_twin.whatif import SCENARIO_KINDS, build_scenario

router = APIRouter(prefix="/api/digital-twin", tags=["digital-twin"])


class WhatIfRequest(BaseModel):
    template_id: str | None = None
    kind: str | None = None
    seed_ids: list[str] = Field(min_length=1)
    horizon_days: int | None = Field(default=None, ge=1)
    rehearse: bool = False


class PredictRequest(BaseModel):
    absent_ids: list[str] = Field(min_length=1)
    horizon_days: int = Field(default=7, ge=1)
    depth: int = Field(default=6, ge=1)


def _scenario_from_request(payload: WhatIfRequest) -> dict:
    """Exactly one of template_id / kind; both missing or both given is
    a 422 from pydantic's point of view, so this raises InvalidInputError
    (also 422) for the ambiguous cases."""
    if bool(payload.template_id) == bool(payload.kind):
        raise InvalidInputError(
            "Provide exactly one of template_id or kind.")
    if payload.template_id is not None:
        try:
            return build_scenario(payload.template_id, payload.seed_ids,
                                  horizon_days=payload.horizon_days)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
    if payload.kind not in SCENARIO_KINDS:
        raise InvalidInputError(
            f"Unknown scenario kind {payload.kind!r}; expected one of {sorted(SCENARIO_KINDS)}.")
    horizon_days = payload.horizon_days if payload.horizon_days is not None else 7
    return {
        "template_id": None,
        "kind": payload.kind,
        "name": payload.kind,
        "seed_ids": list(payload.seed_ids),
        "horizon_days": horizon_days,
    }


@router.get("/{tenant_id}/overview")
async def get_twin_overview(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
):
    """Read-only summary of the live twin for one graph tenant."""
    scope = await scope_for(db, tenant_id, user)
    return await twin_overview(db, scope, continuity_tenant_id=continuity_tenant_id,
                               group_id=group_id)


@router.post("/{tenant_id}/what-if")
async def post_twin_what_if(
    tenant_id: UUID,
    payload: WhatIfRequest,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
    depth: int = Query(default=6, ge=1),
):
    """Run a what-if scenario against the live twin (dry run, read-only).

    The simulation result is deterministic for the same twin
    fingerprint + scenario. Nothing is written, approved, or paid.
    """
    scenario = _scenario_from_request(payload)
    scope = await scope_for(db, tenant_id, user)
    try:
        return await run_whatif_for_scope(
            db, scope, scenario, continuity_tenant_id=continuity_tenant_id,
            group_id=group_id, depth=depth, rehearse=payload.rehearse)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


@router.post("/{tenant_id}/predict-absence")
async def post_twin_predict_absence(
    tenant_id: UUID,
    payload: PredictRequest,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
):
    """Run a horizon absence prediction against the live twin (read-only)."""
    scope = await scope_for(db, tenant_id, user)
    try:
        return await predict_for_scope(
            db, scope, payload.absent_ids, continuity_tenant_id=continuity_tenant_id,
            group_id=group_id, horizon_days=payload.horizon_days, depth=payload.depth)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


__all__ = ["router"]
