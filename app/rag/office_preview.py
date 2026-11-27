"""Page images for Word, PowerPoint and Excel files, through a PDF rendering made by LibreOffice.

PDF pages are rendered directly (`app.rag.pdf.render_page_png`). An Office file has no fixed pages until
something lays it out, so it is converted to PDF once with LibreOffice running headless on this machine,
and the PDF is cached next to the upload (`DocumentStorage.preview_path`) and deleted with it. Nothing leaves
the server. Without LibreOffice the feature says so instead of failing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import textwrap
from pathlib import Path

from app.rag.pdf import PdfError, inspect_pdf
from app.rag.storage import DocumentStorage

logger = logging.getLogger(__name__)

OFFICE_FORMATS = frozenset({"docx", "pptx", "xlsx"})
_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class PreviewError(Exception):
    """The file could not be laid out as pages."""


def find_converter(configured: str = "") -> Path | None:
    """LibreOffice's `soffice`: the configured path, else on PATH, else a standard install location."""
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return next((Path(candidate) for candidate in _CANDIDATES if Path(candidate).is_file()), None)


def ensure_pdf(storage: DocumentStorage, key: str, converter: Path, timeout_seconds: int, max_pages: int) -> tuple[Path, int]:
    """The cached PDF rendering of a stored Office file and its page count. Blocking: run in a thread."""
    target = storage.preview_path(key)
    with _lock(key):
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Work beside the target: the final rename must stay on one drive (system temp is often another).
            with tempfile.TemporaryDirectory(prefix="work-", dir=target.parent) as work:
                produced = _convert(converter, storage.path_for(key), Path(work), timeout_seconds)
                os.replace(produced, target)
    try:
        return target, inspect_pdf(target, max_pages)
    except PdfError as exc:
        target.unlink(missing_ok=True)
        raise PreviewError(exc.message) from None


def render_python_page(storage: DocumentStorage, key: str, file_format: str, page_number: int, width: int,
                       max_pages: int, max_chars: int) -> bytes:
    """Render Office content to a local, readable page image when LibreOffice is unavailable.

    This intentionally provides a content preview (text, tables and worksheet values) rather than claiming
    pixel-perfect office layout. Extraction uses the same Python libraries as ingestion and never runs macros.
    """
    if file_format not in OFFICE_FORMATS or page_number < 1 or page_number > max_pages:
        raise PreviewError("Page not found.")
    target = _python_preview_path(storage, key, file_format, page_number, width)
    with _lock(f"python:{key}:{file_format}:{page_number}:{width}"):
        if target.is_file():
            return target.read_bytes()
        pages = _office_text_pages(storage.path_for(key), file_format, max_pages, max_chars, width)
        if page_number > len(pages):
            raise PreviewError("Page not found.")
        encoded = _render_lines(pages[page_number - 1], width, page_number, len(pages))
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_bytes(encoded)
        os.replace(temp, target)
        return encoded


def _python_preview_path(storage: DocumentStorage, key: str, file_format: str, page: int, width: int) -> Path:
    source = storage.path_for(key)
    return source.parent / "previews" / f"{source.stem}.{file_format}.{page}.{width}.png"


def _office_text_pages(path: Path, file_format: str, max_pages: int, max_chars: int, width: int) -> list[list[str]]:
    lines: list[str] = []
    used = 0

    def add(value: object) -> None:
        nonlocal used
        text = " ".join(str(value or "").split())
        if not text or used >= max_chars:
            return
        text = text[: max_chars - used]
        lines.extend(text.splitlines() or [text])
        used += len(text) + 1

    try:
        if file_format == "docx":
            from docx import Document as WordDocument
            from docx.table import Table
            from docx.text.paragraph import Paragraph

            doc = WordDocument(str(path))
            for block in doc.iter_inner_content():
                if isinstance(block, Paragraph):
                    value = block.text.strip()
                    style = (block.style.name or "") if block.style is not None else ""
                    if value:
                        add(f"{style}: {value}" if style.lower().startswith(("title", "heading")) else value)
                elif isinstance(block, Table):
                    for row in block.rows:
                        add(" | ".join(" ".join(cell.text.split()) for cell in row.cells))
        elif file_format == "pptx":
            from pptx import Presentation

            presentation = Presentation(str(path))
            for number, slide in enumerate(presentation.slides, start=1):
                add(f"Slide {number}")
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        add(shape.text_frame.text)
                    elif getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            add(" | ".join(" ".join(cell.text.split()) for cell in row.cells))
                add("")
        else:
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            try:
                for sheet in workbook.worksheets:
                    add(f"Worksheet: {sheet.title}")
                    for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                        values = [" ".join(str(value).split()) for value in row if value is not None]
                        if values:
                            add(f"{row_number}: " + " | ".join(values))
                        if used >= max_chars:
                            break
                    add("")
            finally:
                workbook.close()
    except Exception as exc:
        logger.warning("Python Office preview failed format=%s error_type=%s", file_format, type(exc).__name__)
        raise PreviewError("This Office document could not be previewed.") from None
    if not lines:
        lines = ["This document contains no previewable text."]
    page_lines = _paginate(lines, max_pages, width)
    return page_lines


def _paginate(lines: list[str], max_pages: int, width: int) -> list[list[str]]:
    per_page = 46
    wrap_width = max(18, int(width / 9))
    pages: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        wrapped = textwrap.wrap(line, width=wrap_width, break_long_words=True, replace_whitespace=True) or [""]
        for part in wrapped:
            if len(current) >= per_page:
                pages.append(current)
                current = []
                if len(pages) >= max_pages:
                    return pages
            current.append(part)
    if current:
        pages.append(current)
    return pages[:max_pages]


def _render_lines(lines: list[str], width: int, page_number: int, page_count: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    width = max(200, min(width, 1600))
    height = round(width * 1.414)
    font = _font(max(12, round(width * 0.02)))
    heading = _font(max(14, round(width * 0.025)), bold=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    margin = max(24, round(width * 0.06))
    draw.text((margin, margin), f"Office preview · page {page_number} of {page_count}", fill="#176b62", font=heading)
    y = margin + 42
    line_height = max(20, round(width * 0.027))
    max_y = height - margin - 24
    for line in lines:
        if y + line_height > max_y:
            break
        draw.text((margin, y), line, fill="#17212b", font=font)
        y += line_height
    buffer = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer.read()


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = (
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _convert(converter: Path, source: Path, work: Path, timeout_seconds: int) -> Path:
    # Its own profile directory: a desktop LibreOffice the admin has open would otherwise take the job, or
    # refuse it, and two conversions would fight over one profile.
    profile = work / "profile"
    out = work / "out"
    out.mkdir()
    command = [
        str(converter),
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out),
        str(source),
    ]
    try:
        subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        raise PreviewError("Laying out the pages took too long.") from None
    except OSError as exc:
        logger.warning("LibreOffice could not be started (%s)", type(exc).__name__)
        raise PreviewError("The page renderer could not be started.") from None
    produced = out / f"{source.stem}.pdf"
    if not produced.is_file():
        raise PreviewError("The page renderer produced no pages for this file.")
    return produced


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())
