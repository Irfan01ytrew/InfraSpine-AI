"""Admin-only status and isolated health checks for the configured OCR engine."""

import asyncio

from fastapi import APIRouter, Depends

from app.api.deps import AppSettings, require_admin
from app.rag.ocr_health import ocr_engine_status, run_ocr_health_check
from app.schemas.ocr import OcrEngineStatusResponse, OcrEngineTestResponse

router = APIRouter(
    prefix="/api/admin/ocr-engine",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=OcrEngineStatusResponse)
async def ocr_engine_info(settings: AppSettings) -> OcrEngineStatusResponse:
    return ocr_engine_status(settings)


@router.post("/test", response_model=OcrEngineTestResponse)
async def test_ocr_engine(settings: AppSettings) -> OcrEngineTestResponse:
    result = await asyncio.to_thread(run_ocr_health_check, settings)
    return OcrEngineTestResponse(**result)
