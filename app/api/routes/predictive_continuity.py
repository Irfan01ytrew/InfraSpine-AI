"""Read-only forecast-record API (Step L slice 3).

``GET /api/predictive-continuity/{tenant_id}/forecasts`` lists the
stored forecasts for a graph tenant (newest first, current records
only unless ``include_superseded=true``); ``GET .../forecasts/{id}``
returns one record. Both are pure reads: no writes, no approvals, no
side effects of any kind. Access is graph membership first (404 for
non-members) then the org-scope ``forecasts.read`` permission (403
for members without it), and every forecast body is filtered to what
the caller may see on the graph.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, DbSession, get_current_user
from app.predictive_continuity.records import get_forecast as _get_forecast
from app.predictive_continuity.records import list_forecasts as _list_forecasts

router = APIRouter(
    prefix="/api/predictive-continuity",
    tags=["predictive-continuity"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/{tenant_id}/forecasts")
async def list_forecasts_route(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    include_superseded: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    items = await _list_forecasts(
        db, tenant_id, user,
        include_superseded=include_superseded, limit=limit, offset=offset)
    return {"tenant_id": str(tenant_id), "forecasts": items, "count": len(items)}


@router.get("/{tenant_id}/forecasts/{record_id}")
async def get_forecast_route(
    tenant_id: uuid.UUID,
    record_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    return await _get_forecast(db, tenant_id, record_id, user)
