"""Synthetic parser fixtures and isolated parser diagnostics for the admin health check."""

import importlib.util
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.rag.parsers.base import ParseError

_DOCLING_FORMATS = frozenset({"pdf", "docx", "pptx"})
_BUILTIN = {
    "pdf": ("Built-in PDF parser (pypdf)", ("pypdf", "pypdfium2")),
    "docx": ("Built-in Word parser (python-docx)", ("docx",)),
    "xlsx": ("Built-in Excel parser (openpyxl)", ("openpyxl",)),
    "pptx": ("Built-in PowerPoint parser (python-pptx)", ("pptx",)),
    "txt": ("InfraSpine text parser", ()),
    "csv": ("InfraSpine CSV parser", ()),
}
_EXTENSIONS = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "txt": ".txt", "csv": ".csv"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _available(modules: tuple[str, ...]) -> bool:
    return all(_module_available(module) for module in modules)


def parser_catalog(settings: Any) -> list[dict[str, Any]]:
    """Return the existing parser selection and its installed-package availability."""
    from app.rag.parsers.registry import docling_runtime_enabled

    docling_available = _module_available("docling")
    docling_enabled = docling_runtime_enabled(settings)
    result = []
    for file_format, (builtin_name, builtin_modules) in _BUILTIN.items():
        uses_docling = docling_runtime_enabled(settings, file_format) and file_format in _DOCLING_FORMATS
        fallback_name = builtin_name if file_format in _DOCLING_FORMATS else None
        result.append(
            {
                "file_format": file_format,
                "extension": _EXTENSIONS[file_format],
                "active_parser": "Docling" if uses_docling else builtin_name,
                "fallback_parser": fallback_name,
                "active_available": docling_available if uses_docling else _available(builtin_modules),
                "fallback_available": _available(builtin_modules) if fallback_name else None,
            }
        )
    return result


def _create_fixture(path: Path, file_format: str) -> None:
    """Write a tiny synthetic file that exercises the parser without user or production data."""
    if file_format == "txt":
        path.write_text("InfraSpine parser health fixture", encoding="utf-8")
        return
    if file_format == "csv":
        path.write_text("Field,Value\nHealth,Passed\n", encoding="utf-8")
        return
    if file_format == "pdf":
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page = writer.add_blank_page(595, 842)
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 10 Tf 50 790 Td (InfraSpine parser health fixture) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        buffer = io.BytesIO()
        writer.write(buffer)
        path.write_bytes(buffer.getvalue())
        return
    if file_format == "docx":
        from docx import Document

        document = Document()
        document.add_paragraph("InfraSpine parser health fixture")
        document.save(path)
        return
    if file_format == "xlsx":
        import openpyxl

        workbook = openpyxl.Workbook()
        workbook.active.append(["Health", "Passed"])
        workbook.save(path)
        return
    if file_format == "pptx":
        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "Health"
        slide.placeholders[1].text = "InfraSpine parser health fixture"
        presentation.save(path)
        return
    raise ValueError("Unsupported parser health format")


def _parse_fixture_isolated(
    path: str,
    file_format: str,
    limits,
    chunk_size: int,
    overlap: int,
    use_docling: bool,
    marker_path: str,
) -> tuple[int | None, int]:
    """The run_isolated target. Marks the real Docling/fallback choice inside the child process."""
    from app.rag.isolation import parse_and_chunk

    marker = Path(marker_path)
    previous_marker = os.environ.get("INFRASPINE_DOCLING_MARKER")
    os.environ["INFRASPINE_DOCLING_MARKER"] = marker_path
    marker.write_text(
        "docling" if use_docling and file_format in _DOCLING_FORMATS else "builtin",
        encoding="utf-8",
    )
    try:
        page_count, chunks, *_ = parse_and_chunk(path, file_format, limits, chunk_size, overlap, use_docling)
        return page_count, len(chunks)
    finally:
        if previous_marker is None:
            os.environ.pop("INFRASPINE_DOCLING_MARKER", None)
        else:
            os.environ["INFRASPINE_DOCLING_MARKER"] = previous_marker


def run_parser_health_check(file_format: str, settings: Any) -> dict[str, Any]:
    """Exercise one configured parser path in the same killable child boundary used by ingestion."""
    from app.rag.isolation import ProcessingCrashed, run_isolated
    from app.rag.parsers.registry import docling_runtime_enabled, limits_from_settings

    started = time.perf_counter()
    docling_requested = docling_runtime_enabled(settings, file_format) and file_format in _DOCLING_FORMATS
    builtin_name = _BUILTIN[file_format][0]
    with tempfile.TemporaryDirectory(prefix="infraspine-parser-health-") as directory:
        root = Path(directory)
        path = root / f"health{_EXTENSIONS[file_format]}"
        marker = root / "parser.txt"

        def selected_parser() -> str:
            try:
                marker_value = marker.read_text(encoding="utf-8")
            except OSError:
                marker_value = ""
            if marker_value == "docling":
                return "Docling"
            if marker_value in {"builtin", "fallback"}:
                return builtin_name
            return "Not reached"

        try:
            _create_fixture(path, file_format)
            page_count, chunk_count = run_isolated(
                _parse_fixture_isolated,
                (
                    str(path),
                    file_format,
                    limits_from_settings(settings),
                    settings.rag_chunk_size,
                    settings.rag_chunk_overlap,
                    docling_requested,
                    str(marker),
                ),
                timeout_seconds=settings.rag_processing_timeout_seconds,
                memory_bytes=settings.rag_parser_memory_mb * 1024 * 1024,
            )
            parser_used = selected_parser()
            if chunk_count < 1:
                raise ParseError("no_text", "The parser returned no searchable text.")
            return {
                "file_format": file_format,
                "ok": True,
                "parser_used": parser_used,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "page_count": page_count,
                "chunk_count": chunk_count,
                "error": None,
            }
        except ParseError as exc:
            return {
                "file_format": file_format,
                "ok": False,
                "parser_used": selected_parser(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "page_count": None,
                "chunk_count": None,
                "error": exc.message,
            }
        except ProcessingCrashed:
            return {
                "file_format": file_format,
                "ok": False,
                "parser_used": selected_parser(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "page_count": None,
                "chunk_count": None,
                "error": "The isolated parser process exited before returning a result.",
            }
        except Exception as exc:
            parser_used = selected_parser()
            if parser_used == "Not reached":
                parser_used = "Docling" if docling_requested else builtin_name
            return {
                "file_format": file_format,
                "ok": False,
                "parser_used": parser_used,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "page_count": None,
                "chunk_count": None,
                "error": f"The parser health check failed ({type(exc).__name__}).",
            }
