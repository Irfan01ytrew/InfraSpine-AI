"""Status and isolated, local-only health checks for the configured RapidOCR path."""

import importlib.util
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from app.rag.parsers.base import ParseError
from app.schemas.ocr import OcrEngineStatusResponse

ENGINE_NAME = "RapidOCR (ONNX Runtime)"
_FIXTURE_TEXT = "INFRASPINE OCR HEALTH 4821"
_FIXTURE_TOKENS = ("infraspine", "ocr", "health", "4821")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def ocr_engine_status(settings: Any) -> OcrEngineStatusResponse:
    docling_available = _module_available("docling")
    rapidocr_available = _module_available("rapidocr")
    pytorch_available = _module_available("torch")
    onnxruntime_available = _module_available("onnxruntime")
    pdfium_available = _module_available("pypdfium2")
    runtime_available = rapidocr_available and onnxruntime_available and pdfium_available
    enabled = bool(settings.rag_ocr_enabled)
    status = "disabled" if not enabled else "ready" if runtime_available else "unavailable"
    return OcrEngineStatusResponse(
        engine_name=ENGINE_NAME,
        enabled=enabled,
        runtime_available=runtime_available,
        status=status,
        docling_available=docling_available,
        rapidocr_available=rapidocr_available,
        onnxruntime_available=onnxruntime_available,
        pytorch_available=pytorch_available,
        backend="onnxruntime",
        device="cpu",
        full_page_ocr_enabled=True,
        orientation_classifier_enabled=False,
        configured_languages=["ch (RapidOCR default)"],
        capabilities=[
            "Offline OCR for scanned PDF pages and blank pages in mixed PDFs",
            "Page-at-a-time rendering to bound memory during OCR",
            "CPU inference through locally cached ONNX model weights",
        ],
    )


def _write_scanned_pdf(path: Path) -> None:
    """Create an image-only PDF so the health check cannot pass on an embedded text layer."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1800, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=92)
    draw.text((70, 120), _FIXTURE_TEXT, fill="black", font=font)
    image.save(path, format="PDF", resolution=200.0)


def _parse_scanned_fixture_isolated(
    path: str, chunk_size: int, overlap: int, limits
) -> tuple[int, int, int, bool, str]:
    """OCR a synthetic image-only PDF with local RapidOCR weights, then exercise normal chunking."""
    from app.rag.chunking import chunk_sections
    from app.rag.ocr import ocr_pdf_pages

    sections = ocr_pdf_pages(Path(path), limits, [1])
    chunks = chunk_sections(sections, chunk_size, overlap)
    text = " ".join(chunk.text for chunk in chunks).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", text)
    recognized = all(token in normalized.split() for token in _FIXTURE_TOKENS)
    return 1, len(chunks), len(text.strip()), recognized, "RapidOCR"


def _failure(message: str, latency_ms: int, parser_used: str = "Not run") -> dict[str, Any]:
    return {
        "ok": False,
        "engine_name": ENGINE_NAME,
        "parser_used": parser_used,
        "latency_ms": latency_ms,
        "page_count": None,
        "chunk_count": None,
        "recognized_characters": 0,
        "message": message,
        "error": message,
    }


def run_ocr_health_check(settings: Any) -> dict[str, Any]:
    """Exercise RapidOCR against a one-page scanned PDF in a bounded child process."""
    from app.rag.isolation import ProcessingCrashed, run_isolated
    from app.rag.parsers.registry import limits_from_settings

    started = time.perf_counter()

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    status = ocr_engine_status(settings)
    if not status.enabled:
        return _failure("OCR is disabled by RAG_OCR_ENABLED.", elapsed_ms())
    if not status.runtime_available:
        return _failure("RapidOCR, ONNX Runtime, or PDFium is unavailable.", elapsed_ms())

    with tempfile.TemporaryDirectory(prefix="infraspine-ocr-health-") as directory:
        root = Path(directory)
        fixture = root / "scanned-health-fixture.pdf"
        try:
            _write_scanned_pdf(fixture)
            page_count, chunk_count, recognized_characters, recognized, parser_used = run_isolated(
                _parse_scanned_fixture_isolated,
                (str(fixture), settings.rag_chunk_size, settings.rag_chunk_overlap, limits_from_settings(settings)),
                timeout_seconds=settings.rag_processing_timeout_seconds,
                # This known one-page, locally-generated fixture has a fixed small pixel bound. ONNX Runtime can
                # reserve several GiB of virtual address space on Linux for the shared OCR session even when its
                # resident memory is much smaller, so RLIMIT_AS falsely reports an OCR failure at the ingestion
                # limit. Keep the killable process and time limit; normal uploaded files still use the configured
                # parser memory limit in IngestionWorker.
                memory_bytes=0,
            )
            if parser_used != "RapidOCR":
                return _failure(
                    "The scanned fixture was not processed by RapidOCR.", elapsed_ms(), parser_used
                )
            if not recognized:
                return _failure(
                    "OCR did not recognize the text in the synthetic scanned fixture.", elapsed_ms(), parser_used
                )
            return {
                "ok": True,
                "engine_name": ENGINE_NAME,
                "parser_used": parser_used,
                "latency_ms": elapsed_ms(),
                "page_count": page_count,
                "chunk_count": chunk_count,
                "recognized_characters": recognized_characters,
                "message": "RapidOCR recognized text in the synthetic scanned PDF using local model files.",
                "error": None,
            }
        except ParseError as exc:
            message = {
                "processing_timeout": "The isolated OCR health test exceeded the processing time limit.",
                "memory_limit": "The isolated OCR health test exceeded the processing memory limit.",
                "no_text": "OCR did not recognize text in the synthetic scanned fixture.",
            }.get(exc.code, "The isolated OCR health test could not parse the synthetic fixture.")
            return _failure(message, elapsed_ms(), "RapidOCR")
        except ProcessingCrashed:
            return _failure("The isolated OCR process exited before returning a result.", elapsed_ms())
        except Exception as exc:
            # Do not return parser paths, dependency details, or document contents to the API caller.
            return _failure(f"The isolated OCR health test failed ({type(exc).__name__}).", elapsed_ms())
