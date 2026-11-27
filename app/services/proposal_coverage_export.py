"""Branded PDF, DOCX, and XLSX exports for ACL-filtered proposal coverage cases."""

from __future__ import annotations

import io
import re
from typing import Literal
from xml.sax.saxutils import escape

from app.schemas.deployment import CompanyProfile
from app.schemas.proposal_coverage import ProposalCoverageDetail

ProposalCoverageExportFormat = Literal["pdf", "docx", "xlsx"]
Export = tuple[bytes, str, str]

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]")


def render_proposal_coverage_export(
    case: ProposalCoverageDetail, profile: CompanyProfile, format: ProposalCoverageExportFormat
) -> Export:
    renderers = {"pdf": _render_pdf, "docx": _render_docx, "xlsx": _render_xlsx}
    filename = f"proposal-coverage-r{case.revision_number}-{str(case.id)[:8]}.{format}"
    return renderers[format](case, profile), _MEDIA_TYPES[format], filename


def _clean(value: object) -> str:
    return _INVALID_XML.sub("", str(value or ""))


def _brand(profile: CompanyProfile) -> str:
    return _clean(profile.brand_name or profile.company_name)


def _requirement_rows(case: ProposalCoverageDetail) -> list[dict[str, str]]:
    rows = []
    for item in case.requirements:
        internal_sources = [
            f"{citation.filename} — {citation.location or f'Page {citation.page_number}'}"
            for citation in item.citations
        ]
        origin_sources = [
            f"{citation.filename} — {citation.location}"
            for citation in item.origin_evidence
        ]
        rows.append({
            "requirement": _clean(item.requirement),
            "assessment": _clean(item.assessment.replace("_", " ").title()),
            "assessment_rationale": _clean(item.assessment_rationale),
            "evidence_status": "Current" if item.evidence_current else "Withheld or stale",
            "answer": _clean(item.answer),
            "internal_sources": _clean("\n".join(internal_sources) or "None"),
            "origin_sources": _clean("\n".join(origin_sources) or "None"),
        })
    return rows


def _pdf_text(value: object) -> str:
    return escape(_clean(value)).replace("\n", "<br/>")


def _render_pdf(case: ProposalCoverageDetail, profile: CompanyProfile) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    output = io.BytesIO()
    brand_color = colors.HexColor(profile.primary_color)
    page_size = landscape(A4)
    document = SimpleDocTemplate(
        output, pagesize=page_size, rightMargin=12 * mm, leftMargin=12 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm, title=f"{_brand(profile)} proposal coverage",
        author="InfraSpine AI",
    )
    base = getSampleStyleSheet()
    body = ParagraphStyle("CoverageBody", parent=base["BodyText"], fontSize=7, leading=9, wordWrap="CJK")
    heading = ParagraphStyle("CoverageHeading", parent=base["Heading2"], textColor=brand_color, spaceBefore=8, spaceAfter=4)
    story = [
        Paragraph(_pdf_text(_brand(profile)), base["Title"]),
        Paragraph("Tender and RFP coverage", base["Heading1"]),
        Paragraph(
            f"Case: {_pdf_text(case.title)} | Status: {_pdf_text(case.status.replace('_', ' ').title())} | "
            f"Revision: {case.revision_number} | Owner: {_pdf_text(case.owner_email)} | "
            f"Department: {_pdf_text(case.department_name or 'Private')}", body,
        ),
        Paragraph(f"Updated {case.updated_at.isoformat()}", body),
        Paragraph("Requirement evidence map", heading),
    ]
    headers = ["Requirement", "Assessment", "Rationale", "Evidence status", "Evidence finding", "Internal sources", "RFP origin"]
    table_data = [[Paragraph(_pdf_text(label), body) for label in headers]]
    table_data.extend([[Paragraph(_pdf_text(row[key]), body) for key in (
        "requirement", "assessment", "assessment_rationale", "evidence_status", "answer", "internal_sources", "origin_sources",
    )] for row in _requirement_rows(case)])
    table = LongTable(
        table_data, repeatRows=1, splitInRow=1,
        colWidths=[42*mm, 21*mm, 41*mm, 24*mm, 62*mm, 42*mm, 41*mm], hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand_color), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D5DD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([table, Spacer(1, 6)])
    for review in case.reviews:
        story.append(Paragraph(
            f"Revision {review.revision_number}: {_pdf_text(review.decision.replace('_', ' ').title())} "
            f"by {_pdf_text(review.reviewer_email)} on {review.reviewed_at.isoformat()} — {_pdf_text(review.rationale)}",
            body,
        ))
    document.build(story)
    return output.getvalue()


def _render_docx(case: ProposalCoverageDetail, profile: CompanyProfile) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, RGBColor

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = section.right_margin = Inches(0.55)
    title = document.add_heading(_brand(profile), 0)
    title.runs[0].font.color.rgb = RGBColor.from_string(profile.primary_color.lstrip("#").upper())
    document.add_heading("Tender and RFP coverage", level=1)
    document.add_paragraph(
        f"{_clean(case.title)} | {_clean(case.status.replace('_', ' ').title())} | Revision {case.revision_number}\n"
        f"Owner: {_clean(case.owner_email)} | Department: {_clean(case.department_name or 'Private')} | "
        f"Updated: {case.updated_at.isoformat()}"
    )
    table = document.add_table(rows=1, cols=7)
    table.style = "Light Shading Accent 1"
    headers = ["Requirement", "Assessment", "Rationale", "Evidence status", "Evidence finding", "Internal sources", "RFP origin"]
    for cell, label in zip(table.rows[0].cells, headers):
        cell.text = label
    for row in _requirement_rows(case):
        cells = table.add_row().cells
        for cell, key in zip(cells, ("requirement", "assessment", "assessment_rationale", "evidence_status", "answer", "internal_sources", "origin_sources")):
            cell.text = row[key]
    document.add_heading("Review history", level=2)
    if case.reviews:
        for review in case.reviews:
            document.add_paragraph(
                f"Revision {review.revision_number}: {review.decision.replace('_', ' ').title()} by "
                f"{_clean(review.reviewer_email)} on {review.reviewed_at.isoformat()} — {_clean(review.rationale)}"
            )
    else:
        document.add_paragraph("No review decisions recorded.")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _render_xlsx(case: ProposalCoverageDetail, profile: CompanyProfile) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Case"
    overview_rows = [
        (_brand(profile), "Tender and RFP coverage"),
        ("Title", _clean(case.title)), ("Status", _clean(case.status)),
        ("Revision", case.revision_number), ("Owner", _clean(case.owner_email)),
        ("Department", _clean(case.department_name or "Private")),
        ("Updated", case.updated_at.isoformat()),
    ]
    for row in overview_rows:
        overview.append(list(row))
    for row in overview.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    overview["A1"].font = Font(size=18, bold=True, color=profile.primary_color.lstrip("#").upper())
    overview.column_dimensions["A"].width = 26
    overview.column_dimensions["B"].width = 70
    sheet = workbook.create_sheet("Requirements")
    headers = ["Requirement", "Assessment", "Rationale", "Evidence status", "Evidence finding", "Internal sources", "RFP origin"]
    sheet.append(headers)
    for row in _requirement_rows(case):
        sheet.append([row[key] for key in (
            "requirement", "assessment", "assessment_rationale", "evidence_status", "answer", "internal_sources", "origin_sources",
        )])
    fill = PatternFill("solid", fgColor=profile.primary_color.lstrip("#").upper())
    for cell in sheet[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, column in enumerate(sheet.columns, start=1):
        width = min(max(max((len(str(cell.value or "")) for cell in column), default=10) + 2, 12), 48)
        sheet.column_dimensions[get_column_letter(index)].width = width
        for cell in column:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    reviews = workbook.create_sheet("Review History")
    reviews.append(["Revision", "Reviewer", "Decision", "Rationale", "Reviewed at"])
    for review in case.reviews:
        reviews.append([review.revision_number, _clean(review.reviewer_email), review.decision, _clean(review.rationale), review.reviewed_at.isoformat()])
    for cell in reviews[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
    reviews.freeze_panes = "A2"
    for index, column in enumerate(reviews.columns, start=1):
        reviews.column_dimensions[get_column_letter(index)].width = min(max(max((len(str(cell.value or "")) for cell in column), default=10) + 2, 12), 48)
        for cell in column:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
