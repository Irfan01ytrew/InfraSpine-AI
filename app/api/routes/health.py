import logging
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings

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


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness check: report database health and whether queued media can be processed."""
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
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
    return JSONResponse(
        content={
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
    )
