"""Admin-facing status and synthetic health-test results for the configured OCR engine."""

from typing import Literal

from pydantic import BaseModel


class OcrEngineStatusResponse(BaseModel):
    engine_name: str
    enabled: bool
    runtime_available: bool
    status: Literal["ready", "disabled", "unavailable"]
    docling_available: bool
    rapidocr_available: bool
    onnxruntime_available: bool
    pytorch_available: bool
    backend: str
    device: str
    full_page_ocr_enabled: bool
    orientation_classifier_enabled: bool
    configured_languages: list[str]
    capabilities: list[str]


class OcrEngineTestResponse(BaseModel):
    ok: bool
    engine_name: str
    parser_used: str
    latency_ms: int
    page_count: int | None
    chunk_count: int | None
    recognized_characters: int
    message: str
    error: str | None
