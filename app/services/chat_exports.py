"""Create short-lived, user-bound downloadable files from the assistant's validated response."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

MAX_CONTENT_CHARS = 40_000
MAX_EXPORT_BYTES = 25 * 1024 * 1024
EXPORT_TTL_SECONDS = 24 * 60 * 60
MEDIA_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def create_chat_export(
    content: str,
    file_format: str,
    root: Path,
    user_id: uuid.UUID,
    signing_key: bytes,
) -> tuple[str, str]:
    """Build a real Office/PDF file and return an expiring token plus safe download name."""
    if file_format not in MEDIA_TYPES:
        raise ValueError("Unsupported chat export format")
    content = content[:MAX_CONTENT_CHARS].strip()
    if not content:
        raise ValueError("There is no assistant content to export")
    sections = _sections(content)
    renderers = {"pptx": _pptx, "docx": _docx, "xlsx": _xlsx, "pdf": _pdf}
    body = renderers[file_format](sections)
    if len(body) > MAX_EXPORT_BYTES:
        raise ValueError("Generated file exceeds the export size limit")

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    _cleanup(root)
    file_id = uuid.uuid4().hex
    path = root / f"{file_id}.{file_format}"
    temp = root / f"{file_id}.{file_format}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    expires = int(time.time()) + EXPORT_TTL_SECONDS
    signature = _signature(file_id, expires, user_id, signing_key)
    return f"{file_id}.{expires}.{signature}", f"infraspine-chat-response.{file_format}"


def resolve_chat_export(token: str, file_format: str, root: Path, user_id: uuid.UUID, signing_key: bytes) -> Path:
    """Validate expiry, user ownership and path containment before opening an export."""
    if file_format not in MEDIA_TYPES:
        raise HTTPException(status_code=404, detail="Export not found or expired.")
    match = re.fullmatch(r"([0-9a-f]{32})\.(\d{10})\.([0-9a-f]{64})", token)
    if not match:
        raise HTTPException(status_code=404, detail="Export not found or expired.")
    file_id, expires_text, supplied = match.groups()
    expires = int(expires_text)
    expected = _signature(file_id, expires, user_id, signing_key)
    if expires < int(time.time()) or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Export not found or expired.")
    base = root.resolve()
    path = (base / f"{file_id}.{file_format}").resolve()
    if path.parent != base or not path.is_file() or path.stat().st_size > MAX_EXPORT_BYTES:
        raise HTTPException(status_code=404, detail="Export not found or expired.")
    return path


def _signature(file_id: str, expires: int, user_id: uuid.UUID, key: bytes) -> str:
    message = f"{file_id}:{expires}:{user_id}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _cleanup(root: Path) -> None:
    cutoff = time.time() - EXPORT_TTL_SECONDS
    for path in root.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _sections(content: str) -> list[tuple[str, list[str]]]:
    lines = content.splitlines()
    title = "InfraSpine AI response"
    groups: list[tuple[str, list[str]]] = []
    current_title = ""
    current: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        heading = re.match(r"^#{1,3}\s+(.+)$", text)
        slide = re.match(r"^(?:slide\s+\d+\s*[:—-]\s*)(.+)$", text, re.IGNORECASE)
        if heading or slide:
            if current_title or current:
                groups.append((current_title or "Details", current))
            current_title = (heading or slide).group(1).strip(" *#")[:150]
            if not groups:
                title = current_title
            current = []
        else:
            current.append(text)
    if current_title or current:
        groups.append((current_title or "Details", current))
    if not groups:
        groups = [("Response", [line.strip() for line in lines if line.strip()])]
    # A single large answer becomes readable, bounded sections. Keep citation strings with the associated lines.
    expanded: list[tuple[str, list[str]]] = []
    for heading, items in groups:
        if not items:
            expanded.append((heading, []))
            continue
        for index in range(0, len(items), 8):
            suffix = f" ({index // 8 + 1})" if len(items) > 8 else ""
            expanded.append((heading + suffix, items[index:index + 8]))
    return [(title[:150], [])] + expanded[:30]


def _clean_line(line: str) -> tuple[str, bool]:
    is_bullet = bool(re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line))
    line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
    # Preserve usable web citations in Office/PDF exports as readable source URLs instead of raw Markdown.
    line = re.sub(r"\[([^\]]+)\]\(<(https?://[^>\s]+)>\)", r"\1 (\2)", line)
    line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", line)
    line = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: m.group(1) or m.group(2), line)
    line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|`([^`]+)`", lambda m: m.group(1) or m.group(2), line)
    return line[:2000], is_bullet


def _pptx(sections: list[tuple[str, list[str]]]) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    brand = RGBColor(23, 107, 98)
    for index, (heading, lines) in enumerate(sections):
        layout = presentation.slide_layouts[0] if index == 0 else presentation.slide_layouts[1]
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = heading
        slide.shapes.title.text_frame.paragraphs[0].font.color.rgb = brand
        slide.shapes.title.text_frame.paragraphs[0].font.size = Pt(30 if index == 0 else 26)
        if index == 0:
            placeholder = slide.placeholders[1] if len(slide.placeholders) > 1 else None
            if placeholder:
                placeholder.text = "Prepared by InfraSpine AI · Source-backed content is cited in each slide"
                placeholder.text_frame.paragraphs[0].font.size = Pt(16)
            continue
        frame = slide.placeholders[1].text_frame
        frame.clear()
        frame.word_wrap = True
        if not lines:
            frame.text = "No additional detail was included."
            continue
        for line in lines:
            value, bullet = _clean_line(line)
            paragraph = frame.add_paragraph() if frame.paragraphs[0].text else frame.paragraphs[0]
            paragraph.text = value
            paragraph.level = 0 if bullet else 1
            paragraph.font.size = Pt(18 if bullet else 15)
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def _docx(sections: list[tuple[str, list[str]]]) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(sections[0][0], 0)
    for heading, lines in sections[1:]:
        document.add_heading(heading, level=1)
        for line in lines:
            value, bullet = _clean_line(line)
            document.add_paragraph(value, style="List Bullet" if bullet else "Normal")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _xlsx(sections: list[tuple[str, list[str]]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Assistant response"
    sheet.append(["Section", "Content"])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="176B62")
    for heading, lines in sections:
        if not lines:
            sheet.append([heading, ""])
        for line in lines:
            value, _ = _clean_line(line)
            sheet.append([heading, value])
            sheet.cell(sheet.max_row, 2).data_type = "s"  # user content must never be interpreted as a formula
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 110
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _pdf(sections: list[tuple[str, list[str]]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    from app.services.report_export import _register_unicode_font, _shape_rtl

    output = io.BytesIO()
    font = _register_unicode_font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ChatTitle", parent=styles["Title"], fontName=font, textColor="#176B62", spaceAfter=12)
    heading = ParagraphStyle("ChatHeading", parent=styles["Heading2"], fontName=font, textColor="#176B62", spaceBefore=10)
    body = ParagraphStyle("ChatBody", parent=styles["BodyText"], fontName=font, leading=15, spaceAfter=5)
    story = [Paragraph(escape(_shape_rtl(sections[0][0])), title),
             Paragraph("Prepared by InfraSpine AI", body), Spacer(1, 4 * mm)]
    for heading_text, lines in sections[1:]:
        story.append(Paragraph(escape(_shape_rtl(heading_text)), heading))
        for line in lines:
            value, bullet = _clean_line(line)
            prefix = "&#8226; " if bullet else ""
            story.append(Paragraph(prefix + escape(_shape_rtl(value)), body))
    SimpleDocTemplate(output, pagesize=A4, title=sections[0][0], author="InfraSpine AI").build(story)
    return output.getvalue()
