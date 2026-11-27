"""Docling extraction for PDF, DOCX and PPTX.

The converter is built once per process and reused for every document in that process.
Isolated parsing still starts a fresh process per document, which is the existing worker
boundary. A failure here is recoverable: the registry falls back to the built-in parser.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from app.rag.parsers.base import (
    ExtractedImage,
    ExtractedTable,
    Locator,
    ParseError,
    ParseLimits,
    Section,
    TextBudget,
    page_locator,
    require_text,
)
from app.rag.parsers.table_structure import (
    Fragment,
    GridCell,
    build_tables,
    grid_text,
    page_table_locator,
    table_blocks,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_converter = None
_converter_pictures = False


class DoclingFailed(Exception):
    """Docling could not extract this file. The built-in parser should try."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def converter_loaded() -> bool:
    return _converter is not None


# Share of the processing timeout Docling may use on one PDF; the rest is left for the built-in parser fallback.
DOCLING_TIME_SHARE = 0.6


def docling_time_budget() -> float:
    from app.core.config import get_settings

    return get_settings().rag_processing_timeout_seconds * DOCLING_TIME_SHARE


def extract_with_docling(
    path: Path, file_format: str, limits: ParseLimits, images: list[ExtractedImage] | None = None,
    tables: list[ExtractedTable] | None = None,
) -> list[Section]:
    """Return sections with the same locator shapes the built-in parsers produce. With `images` (a list to fill),
    pictures in a PDF are also returned as PNGs with their page. With `tables`, the tables of a PDF or DOCX are
    also returned as rows and columns (see `table_structure`); their text sections are whole-row blocks either way."""
    try:
        with _lock:
            result = _get_converter(pictures=images is not None and file_format == "pdf").convert(path)
    except DoclingFailed:
        raise
    except ParseError:
        raise
    except Exception as exc:
        raise DoclingFailed(type(exc).__name__) from exc

    status = getattr(result.status, "value", str(result.status))
    # "partial_success" means Docling dropped some pages or elements. Indexing the rest would silently lose
    # content, so the built-in parser, which reads every page, takes over instead.
    if status != "success":
        raise DoclingFailed(f"{status} ({len(getattr(result, 'errors', None) or [])} errors)")
    document = result.document
    found: list[ExtractedTable] = []
    if file_format == "pdf":
        sections = _pdf_sections(document, limits, found)
        # Docling's RapidOCR backend may fetch Torch weights from ModelScope. Use the application's
        # local-only ONNX OCR path for image-only pages instead.
        from app.core.config import get_settings

        if get_settings().rag_ocr_enabled:
            page_count = len(getattr(document, "pages", {}) or {})
            pages_with_text = {section.locator.position for section in sections if section.text.strip()}
            blank_pages = [page for page in range(1, page_count + 1) if page not in pages_with_text]
            if blank_pages:
                # Docling can omit small digital text (for example a page footer) while still reporting
                # a successful conversion. Recover that text with the built-in bounded PDF extractor
                # before treating the page as a scanned image that needs OCR.
                from app.rag.pdf import extract_pages

                try:
                    fallback_pages = {
                        page.number: page.text
                        for page in extract_pages(path, limits.max_units, limits.max_chars, require_text=False)
                        if page.text.strip()
                    }
                except ParseError:
                    fallback_pages = {}
                recovered = {page: fallback_pages[page] for page in blank_pages if page in fallback_pages}
                if recovered:
                    sections = [
                        section for section in sections
                        if not (
                            section.locator.kind == "page"
                            and section.locator.position in recovered
                            and not section.text.strip()
                        )
                    ]
                    sections.extend(Section(text, page_locator(page)) for page, text in recovered.items())
                    blank_pages = [page for page in blank_pages if page not in recovered]

            if blank_pages:
                from app.rag.ocr import ocr_pdf_pages

                ocr_sections = ocr_pdf_pages(path, limits, blank_pages)
                ocr_by_page = {section.locator.position: section for section in ocr_sections}
                sections = [
                    section for section in sections
                    if not (
                        section.locator.kind == "page"
                        and section.locator.position in ocr_by_page
                        and not section.text.strip()
                    )
                ]
                sections.extend(ocr_sections)
                sections.sort(key=lambda section: section.locator.position)
        if tables is not None and not any(table.structured for table in found):
            # Docling's layout model can occasionally miss ruled digital tables while still extracting the page
            # text correctly, or return an empty placeholder grid. Use a line-based CPU parser as a bounded
            # fallback in either case; replace the unusable Docling tables only when the fallback gets a header.
            fragments = _pdfplumber_fragments(path, limits, sections)
            if fragments:
                fallback_tables = build_tables(fragments, "page")
                if any(table.structured for table in fallback_tables):
                    found = fallback_tables
                    budget = TextBudget(limits.max_chars * 2)
                    by_page: dict[int, list[Section]] = {}
                    for section in sections:
                        if not section.locator.attrs.get("table"):
                            by_page.setdefault(section.locator.position, []).append(section)
                    for fragment in fragments:
                        if fragment.table is None:
                            continue
                        for block in table_blocks(fragment, limits.section_chars):
                            text = budget.take(block.text)
                            if text:
                                by_page.setdefault(fragment.position, []).append(
                                    Section(text, page_table_locator(block, fragment.table))
                                )
                    sections = [section for page in sorted(by_page) for section in by_page[page]]
    elif file_format == "docx":
        sections = _docx_sections(document, limits, found)
    elif file_format == "pptx":
        sections = _pptx_sections(document, limits)
    else:
        raise DoclingFailed(f"unsupported format {file_format}")
    if not any(section.text.strip() for section in sections):
        raise DoclingFailed("no text")
    if images is not None and file_format == "pdf":
        images.extend(_pdf_images(document))
    if tables is not None:
        tables.extend(found)
    _mark("docling")
    return sections


def _pdf_images(document) -> list[ExtractedImage]:
    """Pictures Docling found (charts, diagrams, screenshots), skipping icons; at most the configured number."""
    import io

    from app.core.config import get_settings

    settings = get_settings()
    found: list[ExtractedImage] = []
    heading: dict[int, str] = {}  # last heading seen on each page: a chart's title is often laid out outside it
    for item, _level in document.iterate_items(included_content_layers=_layers()):
        if _label(item) in {"title", "section_header"}:
            heading[_page(item) or 1] = _plain(item)
            continue
        if _label(item) != "picture" or len(found) >= settings.rag_image_max_per_document:
            continue
        try:
            image = item.get_image(document)
        except Exception:  # a picture without pixels (none generated) is skipped, not fatal
            image = None
        if image is None or min(image.size) < settings.rag_image_min_pixels:
            continue
        image = image.convert("RGB")
        image.thumbnail((1600, 1600))  # enough to read a chart; keeps requests small
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        page = _page(item) or 1
        caption = " ".join((item.caption_text(document) or "").split()) or heading.get(page, "")
        found.append(ExtractedImage(page, buffer.getvalue(), caption[:300]))
    return found


def _mark(kind: str) -> None:
    """Test hook. Production leaves INFRASPINE_DOCLING_MARKER unset, so this writes nothing."""
    marker = os.environ.get("INFRASPINE_DOCLING_MARKER")
    if marker:
        Path(marker).write_text(kind, encoding="utf-8")


def _get_converter(pictures: bool = False):
    """One CPU converter per process. PDF layout models load on the first PDF, not per chunk. With `pictures`,
    PDF pictures keep their pixels; the converter is rebuilt only when that choice changes (rare: it follows
    the admin's image sharing authorization), so at most one converter's models are loaded."""
    global _converter, _converter_pictures
    if _converter is not None and _converter_pictures == pictures:
        return _converter
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise DoclingFailed("docling is not installed") from exc

    options = PdfPipelineOptions()
    options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice.CPU, num_threads=max(1, min(8, os.cpu_count() or 1))
    )
    # OCR is handled by app.rag.ocr, which verifies local ONNX weights and never downloads models.
    options.do_ocr = False
    options.do_table_structure = True
    # Table-heavy PDFs take about 12 s per page on CPU (a 100-page, 200-table file needed 21 minutes). When the
    # processing timeout killed such a parse, nothing was indexed. Docling stops at this budget instead and
    # reports partial success, so the built-in parser can still read the whole file in the time that is left.
    options.document_timeout = docling_time_budget()
    if pictures:
        # Keep each picture's pixels (cropped from the rendered page) so it can be described.
        options.generate_picture_images = True
        options.images_scale = 1.5
    _converter = None  # release the previous converter's models before loading new ones
    _converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    _converter_pictures = pictures
    logger.info("Docling converter ready (CPU, formats PDF DOCX PPTX)")
    return _converter


def _layers():
    from docling_core.types.doc import ContentLayer

    return {ContentLayer.BODY, ContentLayer.NOTES}


def _label(item) -> str:
    label = getattr(item, "label", "")
    return getattr(label, "value", str(label))


def _page(item) -> int | None:
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    return int(prov[0].page_no)


def _plain(item) -> str:
    return " ".join((getattr(item, "text", "") or "").split())


def _table_text(item) -> str:
    lines: list[str] = []
    for row in item.data.grid:
        cells: list[str] = []
        for cell in row:
            text = " ".join(cell.text.split())
            if text and (not cells or cells[-1] != text):
                cells.append(text)
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _grid(item) -> list[list[GridCell]]:
    """Docling's table grid (a spanning cell repeats in every column it covers, with its own start column)."""
    return [
        [
            GridCell(
                cell.text or "",
                bool(getattr(cell, "column_header", False)),
                int(getattr(cell, "start_col_offset_idx", column)),
            )
            for column, cell in enumerate(row)
        ]
        for row in item.data.grid
    ]


def _pdf_sections(document, limits: ParseLimits, tables: list[ExtractedTable]) -> list[Section]:
    """One text section per page (blank pages too), and each table's rows as whole-row blocks on their own pages,
    in reading order. `tables` receives the document's tables as rows and columns."""
    page_count = len(getattr(document, "pages", {}) or {})
    parts: dict[int, list[str | Fragment]] = {}
    heading = ""
    after_table = False
    for item, _level in document.iterate_items(included_content_layers=_layers()):
        page = _page(item) or 1
        if _label(item) == "table":
            parts.setdefault(page, []).append(Fragment(page, _grid(item), heading, continues=after_table))
            after_table = True
            page_count = max(page_count, page)
            continue
        text = _plain(item)
        if not text:
            continue
        if _label(item) in {"title", "section_header"}:
            heading = text
        parts.setdefault(page, []).append(text)
        after_table = False
        page_count = max(page_count, page)
    if page_count > limits.max_units:
        raise ParseError("too_many_pages", f"The PDF has {page_count} pages; the limit is {limits.max_units}.")
    fragments = [part for page in sorted(parts) for part in parts[page] if isinstance(part, Fragment)]
    tables.extend(build_tables(fragments, "page"))
    budget = TextBudget(limits.max_chars)
    table_budget = TextBudget(limits.max_chars * 2)  # row labels and repeated column names add to the raw text
    sections: list[Section] = []
    for number in range(1, page_count + 1):
        texts: list[str] = []
        placed = False
        for part in parts.get(number, []):
            if isinstance(part, str):
                texts.append(part)
                continue
            blocks = table_blocks(part, limits.section_chars)
            if not blocks:  # no data rows (for example a header only): its text stays with the page
                texts.append(grid_text(part))
                continue
            if texts:
                sections.append(Section(budget.take("\n".join(texts)), page_locator(number)))
                texts, placed = [], True
            for block in blocks:
                sections.append(Section(table_budget.take(block.text), page_table_locator(block, part.table)))
                placed = True
        if texts or not placed:  # every page has a section, so the page count stays the highest page
            sections.append(Section(budget.take("\n".join(texts)), page_locator(number)))
    return sections


def _pdfplumber_fragments(path: Path, limits: ParseLimits, sections: list[Section]) -> list[Fragment]:
    """Extract missed ruled PDF tables into the same bounded, page-aware structure as Docling tables."""
    import pdfplumber

    titles: dict[int, str] = {}
    for section in sections:
        if section.locator.kind != "page" or not section.text.strip():
            continue
        first_line = next((line.strip() for line in section.text.splitlines() if line.strip()), "")
        if first_line and not first_line.casefold().startswith("page "):
            titles.setdefault(section.locator.position, first_line[:300])

    fragments: list[Fragment] = []
    last_layout: tuple[int, str, float, float, int] | None = None
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages[: limits.max_units], start=1):
            edges = page.edges
            vertical = [edge for edge in edges if abs(edge["x1"] - edge["x0"]) <= 1.0]
            horizontal = [edge for edge in edges if abs(edge["bottom"] - edge["top"]) <= 1.0]
            if len(vertical) < 3 or len(horizontal) < 2:
                continue
            for table in page.find_tables(table_settings={
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            }):
                grid = table.extract()
                if not grid:
                    continue
                # pdfplumber returns one cell per column. Unlike Docling's spanning-cell grid, each value starts
                # at its own column; preserve that position for table header detection and row reconstruction.
                cells = [
                    [GridCell(cell or "", start_col=column) for column, cell in enumerate(row)]
                    for row in grid
                ]
                width = max((len(row) for row in cells), default=0)
                if width < 2 or len(cells) < 2:
                    continue
                x0, _top, x1, _bottom = table.bbox
                title = titles.get(page_number, "")
                if not title and last_layout and last_layout[0] == page_number - 1:
                    # A title printed once on the first page is often absent from a table's continuation page.
                    title = last_layout[1]
                continues = bool(
                    last_layout
                    and last_layout[0] == page_number - 1
                    and last_layout[1].casefold() == title.casefold()
                    and abs(last_layout[2] - x0) <= 8
                    and abs(last_layout[3] - x1) <= 8
                    and last_layout[4] == width
                )
                fragments.append(Fragment(page_number, cells, title, continues=continues))
                last_layout = (page_number, title, x0, x1, width)
    return fragments


def _docx_sections(document, limits: ParseLimits, tables: list[ExtractedTable]) -> list[Section]:
    budget = TextBudget(limits.max_chars)
    table_budget = TextBudget(limits.max_chars * 2)  # row labels and repeated column names add to the raw text
    sections: list[Section] = []
    path_stack: list[tuple[int, str]] = []
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        headings = [heading for _, heading in path_stack]
        if not text and not headings:
            return
        label = " > ".join(headings) if headings else "Document start"
        heading_line = f"Section: {label}\n" if headings else ""
        sections.append(
            Section(
                heading_line + text,
                Locator(
                    "section",
                    len(sections) + 1,
                    label,
                    {"heading_path": headings, "section": len(sections) + 1},
                ),
            )
        )

    def add_table(item) -> None:
        """The table's rows as whole-row sections under the current heading, after the text before it."""
        nonlocal body
        headings = [heading for _, heading in path_stack]
        label = " > ".join(headings) if headings else "Document start"
        fragment = Fragment(len(sections) + 1, _grid(item), headings[-1] if headings else "")
        found = build_tables([fragment], "section", start_index=len(tables) + 1)
        if not fragment.rows:  # no data rows (for example a header only): its text stays in the section
            if text := grid_text(fragment):
                body.append(budget.take(text))
            return
        if body:
            flush()
            body = []
        tables.extend(found)
        for block in table_blocks(fragment, limits.section_chars):
            rows = f"row {block.first_row}" if block.first_row == block.last_row else f"rows {block.first_row}–{block.last_row}"
            number = len(sections) + 1
            sections.append(
                Section(
                    table_budget.take(block.text),
                    Locator(
                        "section", number, f"{label} · Table {fragment.table.index}, {rows}",
                        {"heading_path": headings, "section": number, "table": fragment.table.index,
                         "rows": [block.first_row, block.last_row]},
                    ),
                )
            )

    for item, _level in document.iterate_items(included_content_layers=_layers()):
        label = _label(item)
        if label == "table":
            add_table(item)
            continue
        text = _plain(item)
        if not text:
            continue
        text = budget.take(text)
        if label in {"section_header", "title"}:
            if body:
                flush()
            body = []
            level = int(getattr(item, "level", 1) or 1)
            path_stack = [(lvl, heading) for lvl, heading in path_stack if lvl < level] + [(level, text)]
            continue
        body.append(text)
    flush()
    return require_text(sections)


def _pptx_sections(document, limits: ParseLimits) -> list[Section]:
    slides: dict[int, dict[str, list[str] | str]] = {}

    def bucket(page: int) -> dict[str, list[str] | str]:
        return slides.setdefault(page, {"title": "", "parts": [], "notes": []})

    for item, _level in document.iterate_items(included_content_layers=_layers()):
        page = _page(item)
        if page is None:
            continue
        label = _label(item)
        layer = getattr(getattr(item, "content_layer", ""), "value", str(getattr(item, "content_layer", "")))
        if label == "table":
            text = _table_text(item)
            if text:
                bucket(page)["parts"].append(text)  # type: ignore[union-attr]
            continue
        text = _plain(item)
        if not text:
            continue
        if layer == "notes":
            bucket(page)["notes"].append(text)  # type: ignore[union-attr]
            continue
        if label in {"title", "section_header"} and not bucket(page)["title"]:
            bucket(page)["title"] = text
            continue
        bucket(page)["parts"].append(text)  # type: ignore[union-attr]

    if not slides:
        raise DoclingFailed("no slides")
    last = max(slides)
    if last > limits.max_units:
        raise ParseError("too_many_slides", f"The presentation has {last} slides; the limit is {limits.max_units}.")
    budget = TextBudget(limits.max_chars)
    sections: list[Section] = []
    for number in sorted(slides):
        slide = slides[number]
        title = str(slide["title"])
        parts = list(slide["parts"])  # type: ignore[arg-type]
        parts.extend(f"Speaker notes: {note}" for note in slide["notes"])  # type: ignore[union-attr]
        if not title and not parts:
            continue
        heading = f"Slide {number}" + (f": {title}" if title else "")
        text = budget.take(heading + ("\n" + "\n".join(parts) if parts else ""))
        label = f"Slide {number}" + (f" – {title}" if title else "")
        sections.append(Section(text, Locator("slide", number, label, {"slide": number, "title": title})))
    return require_text(sections)
