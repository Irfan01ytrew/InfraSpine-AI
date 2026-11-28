"""Admin-only status and isolated health checks for the configured OCR engine."""

import asyncio

from fastapi import APIRouter, Depends, Request

from app.api.deps import AppSettings, DbSession, PlatformAdminUser, audit_context, require_platform_admin
from app.rag.ocr_health import ocr_engine_status, run_ocr_health_check
from app.schemas.ocr import OcrEngineStatusResponse, OcrEngineTestResponse
from app.services import audit

router = APIRouter(
    prefix="/api/admin/ocr-engine",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


@router.get("", response_model=OcrEngineStatusResponse)
async def ocr_engine_info(settings: AppSettings) -> OcrEngineStatusResponse:
    return ocr_engine_status(settings)


@router.post("/test", response_model=OcrEngineTestResponse)
async def test_ocr_engine(
    settings: AppSettings, request: Request, db: DbSession, admin: PlatformAdminUser
) -> OcrEngineTestResponse:
    result = await asyncio.to_thread(run_ocr_health_check, settings)
    response = OcrEngineTestResponse(**result)
    audit.record(
        db,
        audit_context(request, admin),
        "admin.ocr_engine_tested",
        target=response.engine_name,
        details={"ok": response.ok, "parser_used": response.parser_used,
                 "latency_ms": response.latency_ms},
        outcome="success" if response.ok else "failure",
    )
    await db.commit()
    return response
