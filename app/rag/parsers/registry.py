"""The parser registry: which formats exist, how a file is recognised, and which parser reads it.

Adding a format (OCR'd images, e-mail, connector content) means adding a parser here; chunking, embedding,
retrieval and citations only see `Section`s and `Locator`s.

PDF, DOCX and PPTX try Docling first. The format parsers below stay the fallback.
"""

import logging
import os
import sys
from pathlib import Path

from app.rag.parsers.base import (
    ExtractedImage,
    ExtractedTable,
    Inspection,
    ParseError,
    Parser,
    ParseLimits,
    Section,
    page_locator,
)
from app.rag.parsers.detect import detect_format, unsupported
from app.rag.parsers.docx_parser import DocxParser
from app.rag.parsers.image_parser import IMAGE_FORMATS, JpegParser, PngParser, WebpParser
from app.rag.parsers.media_parser import AudioParser, VideoParser
from app.rag.parsers.pptx_parser import PptxParser
from app.rag.parsers.text_parser import CsvParser, HtmlParser, JsonParser, MarkdownParser, TextParser
from app.rag.parsers.xlsx_parser import XlsxParser
from app.rag.parsers.legacy_office import DocParser, XlsParser, PptParser
from app.rag.pdf import PageText, PdfError, extract_pages, inspect_pdf

logger = logging.getLogger(__name__)

_DOCLING_FORMATS = frozenset({"pdf", "docx", "pptx"})


class PdfParser:
    file_format = "pdf"
    extensions = (".pdf",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        return inspect_pdf(path, limits.max_units)

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        from app.core.config import get_settings

        settings = get_settings()
        pages = extract_pages(path, limits.max_units, limits.max_chars, require_text=not settings.rag_ocr_enabled)
        if settings.rag_ocr_enabled:
            blank_pages = [page.number for page in pages if not page.text.strip()]
            if blank_pages:
                from app.rag.ocr import ocr_pdf_pages

                ocr_sections = ocr_pdf_pages(path, limits, blank_pages)
                recognized = {section.locator.position: section.text for section in ocr_sections}
                pages = [PageText(page.number, recognized.get(page.number, "") if not page.text.strip() else page.text)
                         for page in pages]
        if not any(page.text.strip() for page in pages):
            raise PdfError("no_text", "No extractable text found. OCR did not recognize any page content.")
        return [Section(p.text, page_locator(p.number)) for p in pages]


PARSERS: dict[str, Parser] = {
    parser.file_format: parser
    for parser in (
        PdfParser(), DocxParser(), XlsxParser(), PptxParser(), TextParser(), CsvParser(), MarkdownParser(),
        JsonParser(), HtmlParser(), PngParser(), JpegParser(), WebpParser(),
        AudioParser(), VideoParser(), DocParser(), XlsParser(), PptParser(),
    )
}
EXTENSIONS: dict[str, str] = {ext: parser.file_format for parser in PARSERS.values() for ext in parser.extensions}
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(EXTENSIONS)
FILE_FORMATS: tuple[str, ...] = tuple(PARSERS)


def limits_from_settings(settings) -> ParseLimits:
    return ParseLimits(
        max_units=settings.rag_max_pages,
        max_chars=settings.rag_max_extracted_chars,
        max_unpacked_bytes=settings.rag_max_unpacked_mb * 1024 * 1024,
        section_chars=settings.rag_chunk_size,
    )


def format_for_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise unsupported(f"Supported file types: {', '.join(SUPPORTED_EXTENSIONS)}.")
    return EXTENSIONS[suffix]


def inspect_document(path: Path, filename: str, limits: ParseLimits) -> Inspection:
    """Upload-time check: content must match the extension, and the file must open within limits."""
    file_format = detect_format(path, format_for_filename(filename), limits.max_unpacked_bytes)
    return Inspection(file_format, PARSERS[file_format].inspect(path, limits))


def parse_document(
    path: Path, file_format: str, limits: ParseLimits, *, use_docling: bool = True,
    images: list[ExtractedImage] | None = None, tables: list[ExtractedTable] | None = None,
) -> list[Section]:
    """Worker-time extraction. The content is detected again, so a parser only ever sees its own format.
    `use_docling=False` goes straight to the built-in parser (the worker retries that way after a crash).
    `images`, when given, receives the document's pictures (Docling PDFs; the built-in parsers find none). An image
    file is its own picture and has no text: it needs `images`. `tables`, when given, receives the tables of a PDF
    or DOCX as rows and columns (Docling only: the built-in parsers keep tables as text)."""
    if file_format not in PARSERS:
        raise unsupported()
    detect_format(path, file_format, limits.max_unpacked_bytes)
    if file_format in IMAGE_FORMATS:
        if images is not None:
            images.append(PARSERS[file_format].picture(path))
        return PARSERS[file_format].parse(path, limits)
    if file_format in _DOCLING_FORMATS and use_docling and _docling_enabled(file_format):
        from app.rag.parsers.docling_extract import DoclingFailed, extract_with_docling

        extra = {name: value for name, value in (("images", images), ("tables", tables)) if value is not None}
        try:
            return extract_with_docling(path, file_format, limits, **extra)
        except DoclingFailed as exc:
            logger.warning(
                "Docling could not extract format=%s error_type=%s; using the built-in parser.",
                file_format, type(exc).__name__,
            )
            _mark_docling("fallback")
            for found in extra.values():
                found.clear()
        except ParseError:
            raise
    elif file_format == "pdf" and use_docling and not _docling_enabled(file_format):
        # Keep the diagnostic marker honest: this platform uses the supported built-in fallback.
        _mark_docling("fallback")
    return PARSERS[file_format].parse(path, limits)


def docling_runtime_enabled(settings=None, file_format: str | None = None) -> bool:
    """The native Docling PDF pipeline is unstable on Windows; Linux container PDF parsing remains supported."""
    from app.core.config import get_settings

    selected = settings if settings is not None else get_settings()
    if not selected.rag_use_docling:
        return False
    return not (file_format == "pdf" and sys.platform == "win32")


def _docling_enabled(file_format: str) -> bool:
    return docling_runtime_enabled(file_format=file_format)


def _mark_docling(kind: str) -> None:
    marker = os.environ.get("INFRASPINE_DOCLING_MARKER")
    if marker:
        Path(marker).write_text(kind, encoding="utf-8")
