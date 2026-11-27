"""Module 11 read-only operations API; shares the existing administrator/MFA boundary."""

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import AppSettings, require_admin
from app.services.infrastructure import InfrastructureMonitor

router = APIRouter(prefix="/api/admin/infrastructure", tags=["infrastructure"], dependencies=[Depends(require_admin)])


@router.get("")
async def overview(request: Request, response: Response, settings: AppSettings) -> dict:
    response.headers["Cache-Control"] = "no-store"
    monitor = getattr(request.app.state, "infrastructure_monitor", None)
    if monitor is None:
        monitor = request.app.state.infrastructure_monitor = InfrastructureMonitor()
    return await monitor.snapshot(request.app, settings)
