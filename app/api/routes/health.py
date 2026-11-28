import json
import logging
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings
from app.ops.db_health import collect_db_health

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str
    llm_mode: Literal["database"] = "database"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check: no database or provider calls. Reveals no provider details."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
    )


async def _safe_collect_db_health(engine):
    """Run the Step AO collector, returning None when it cannot run.

    The collector needs a real engine (its queries use ``scalar()``);
    a fake or minimal engine raises. In that case the route keeps the
    existing SELECT-1 behaviour and reports no detail at all — the
    response stays byte-identical to the pre-AO shape, so older
    clients and the existing readiness contract tests keep passing.
    A snapshot whose ``reachable`` flag is False is also treated as
    unmeasurable here: the SELECT-1 gate above already proved the
    database reachable, so a failed collector means the *collector*
    could not run, not that the database is down.
    """
    try:
        snapshot = await collect_db_health(engine)
    except Exception as exc:  # noqa: BLE001 - any collector failure is "cannot measure"
        logger.warning("Database health collector could not run: %s", type(exc).__name__)
        return None
    if not snapshot.get("reachable"):
        logger.warning(
            "Database health collector reported unreachable: %s", snapshot.get("error")
        )
        return None
    return snapshot


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness check: report database health and whether queued media can be processed.

    Step AO adds a ``database_detail`` section (from the read-only
    collector in ``app.ops.db_health``) alongside the existing
    ``"database": "ok"`` string, which is kept for backwards
    compatibility. A critical overall band returns 503 with
    ``status: "degraded"``; a warning is reported in the detail but
    does not take the app down. Missing signals report ``unknown``,
    never fake ok. Threshold breaches are logged as structured lines
    (there is no platform audit principal in this route).
    """
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
    detail = await _safe_collect_db_health(request.app.state.engine)
    if detail is not None and detail.get("overall") in ("warning", "critical"):
        logger.warning(
            "Database health %s: %s",
            detail["overall"],
            json.dumps(
                {
                    "connections": detail["connections"]["band"],
                    "longest_query": detail["longest_query_band"],
                    "bloat": detail["bloat_band"],
                    "locks": detail["locks"]["band"],
                    "replication": detail["replication"]["band"],
                },
                sort_keys=True,
            ),
        )
    if detail is not None and detail.get("overall") == "critical":
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "ok", "database_detail": detail},
        )
    settings = get_settings()
    try:
        from app.rag.ingestion import workers_online

        async with request.app.state.sessionmaker() as db:
            active_workers = await workers_online(db, settings)
    except Exception as exc:
        logger.error("Ingestion readiness check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "database": "ok",
                "ingestion": {"mode": settings.rag_ingestion_mode, "status": "unknown", "workers_online": 0},
            },
        )
    body: dict = {
        "status": "ready",
        "database": "ok",
        # "owner" writes the vector index; "replica" is an additional API process serving from memory.
        "index_role": getattr(getattr(request.app.state, "rag", None), "index_role", None),
        "ingestion": {
            "mode": settings.rag_ingestion_mode,
            "status": "available" if active_workers else "waiting_for_worker",
            "workers_online": active_workers,
        },
    }
    if detail is not None:
        body["database_detail"] = detail
    return JSONResponse(content=body)
