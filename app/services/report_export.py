"""Editable, aggregate-only administrative report exports.

The exporters receive the same validated report model used by the dashboard. They never query conversations or
include questions, answers, filenames, users, or provider credentials in downloadable files.
"""

from __future__ import annotations

import csv
import io
import unicodedata
from collections.abc import Callable
from typing import Literal
from xml.sax.saxutils import escape

from app.schemas.deployment import CompanyProfile
from app.schemas.reports import UsageReport

ReportFormat = Literal["csv", "pdf", "docx", "xlsx", "pptx"]
Export = tuple[bytes, str, str]

_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def render_usage_export(report: UsageReport, profile: CompanyProfile, format: ReportFormat) -> Export:
    """Return an actual downloadable file and fixed, non-user-controlled filename extension."""
    renderers: dict[str, Callable[[UsageReport, CompanyProfile], bytes]] = {
        "csv": _render_csv,
        "pdf": _render_pdf,
        "docx": _render_docx,
        "xlsx": _render_xlsx,
        "pptx": _render_pptx,
    }
    body = renderers[format](report, profile)
    return body, _MEDIA_TYPES[format], f"infraspine-usage-{report.summary.days}-days.{format}"


def _brand(profile: CompanyProfile) -> str:
    return profile.brand_name.strip() or profile.company_name.strip()


def _contains_rtl(value: str) -> bool:
    return any(unicodedata.bidirectional(character) in {"R", "AL"} for character in value)


def _apply_docx_rtl(document) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    for section in document.sections:
        paragraphs.extend(section.header.paragraphs)
        paragraphs.extend(section.footer.paragraphs)
    for paragraph in paragraphs:
        if not _contains_rtl(paragraph.text):
            continue
        p_properties = paragraph._p.get_or_add_pPr()
        if p_properties.find(qn("w:bidi")) is None:
            bidi = OxmlElement("w:bidi")
            bidi.set(qn("w:val"), "1")
            p_properties.append(bidi)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in paragraph.runs:
            r_properties = run._element.get_or_add_rPr()
            if r_properties.find(qn("w:rtl")) is None:
                rtl = OxmlElement("w:rtl")
                rtl.set(qn("w:val"), "1")
                r_properties.append(rtl)
            fonts = r_properties.rFonts
            if fonts is None:
                fonts = OxmlElement("w:rFonts")
                r_properties.insert(0, fonts)
            fonts.set(qn("w:ascii"), "Arial")
            fonts.set(qn("w:hAnsi"), "Arial")
            fonts.set(qn("w:cs"), "Arial")
            language = r_properties.find(qn("w:lang"))
            if language is None:
                language = OxmlElement("w:lang")
                r_properties.append(language)
            language.set(qn("w:val"), "ar-SA")
            language.set(qn("w:bidi"), "ar-SA")


def _summary_rows(report: UsageReport) -> list[tuple[str, str]]:
    summary = report.summary
    latency = "Unavailable" if summary.latency_p50_ms is None else f"{summary.latency_p50_ms:.1f} ms"
    p95 = "Unavailable" if summary.latency_p95_ms is None else f"{summary.latency_p95_ms:.1f} ms"
    return [
        ("Questions", f"{summary.questions:,}"),
        ("Model calls", f"{summary.llm_calls:,}"),
        ("Errors", f"{summary.errors:,}"),
        ("Refusals", f"{summary.refused:,}"),
        ("Helpful feedback", f"{summary.helpful:,}"),
        ("Not helpful", f"{summary.not_helpful:,}"),
        ("Median latency", latency),
        ("p95 latency", p95),
        ("Prompt tokens", f"{summary.prompt_tokens:,}"),
        ("Completion tokens", f"{summary.completion_tokens:,}"),
        ("General answers", f"{summary.general:,}"),
        ("Period length", f"{summary.days} days"),
    ]


def _provenance(report: UsageReport) -> str:
    return (
        f"Source: aggregate RAG trace and feedback metadata, {report.period_start.isoformat()} through "
        f"{report.period_end.isoformat()}. Generated at {report.generated_at.isoformat()}. "
        "Question text, answer text, usernames, and document content are excluded."
    )


def _safe_csv_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r", "\n")) or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def _render_csv(report: UsageReport, profile: CompanyProfile) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    def write_row(values) -> None:
        writer.writerow([_safe_csv_value(value) for value in values])

    write_row([_brand(profile), "Usage report"])
    write_row(["Period start", report.period_start.isoformat()])
    write_row(["Period end", report.period_end.isoformat()])
    write_row(["Generated at", report.generated_at.isoformat()])
    writer.writerow([])
    write_row(["Summary metric", "Value"])
    for row in _summary_rows(report):
        write_row(row)
    writer.writerow([])
    write_row(["Daily activity"])
    write_row(["Date", "Questions", "Errors", "Refusals", "Helpful", "Not helpful"])
    for row in report.daily:
        write_row([row.day.isoformat(), row.questions, row.errors, row.refused, row.helpful, row.not_helpful])
    writer.writerow([])
    write_row(["Provider usage"])
    write_row(["Provider", "Model", "Calls", "Median latency ms", "p95 latency ms"])
    for row in report.providers:
        write_row([row.provider, row.model or "", row.calls, row.latency_p50_ms or "", row.latency_p95_ms or ""])
    writer.writerow([])
    write_row([_provenance(report)])
    return output.getvalue().encode("utf-8-sig")



def _register_unicode_font() -> str:
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for candidate in [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(candidate):
            try:
                pdfmetrics.registerFont(TTFont("InfraSpineUnicode", candidate))
                return "InfraSpineUnicode"
            except Exception:
                pass
    return "Helvetica"


def _shape_rtl(text: str) -> str:
    if not text:
        return ""
    if any("\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" or "\u08A0" <= ch <= "\u08FF" or "\uFB50" <= ch <= "\uFDFF" or "\uFE70" <= ch <= "\uFEFF" for ch in text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text

def _render_pdf(report: UsageReport, profile: CompanyProfile) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    output = io.BytesIO()
    brand_color = colors.HexColor(profile.primary_color)
    page_size = landscape(A4)
    document = SimpleDocTemplate(
        output, pagesize=page_size, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=17 * mm, bottomMargin=18 * mm, title=f"{_brand(profile)} usage report",
        author="InfraSpine AI",
    )
    font_name = _register_unicode_font()
    base = getSampleStyleSheet()
    title = ParagraphStyle("ReportTitle", parent=base["Title"], fontName=font_name, textColor=brand_color, alignment=TA_LEFT, spaceAfter=4)
    subtitle = ParagraphStyle("ReportSubtitle", parent=base["Normal"], fontName=font_name, textColor=colors.HexColor("#475467"), spaceAfter=10)
    heading = ParagraphStyle("ReportHeading", parent=base["Heading2"], fontName=font_name, textColor=brand_color, spaceBefore=10, spaceAfter=5)
    small = ParagraphStyle("ReportSmall", parent=base["BodyText"], fontName=font_name, fontSize=8, leading=10)
    story = [
        Paragraph(escape(_shape_rtl(_brand(profile))), title),
        Paragraph("Usage and reliability report", base["Heading1"]),
        Paragraph(
            f"{report.period_start.date().isoformat()} to {report.period_end.date().isoformat()} "
            f"&nbsp; | &nbsp; Generated {report.generated_at.isoformat()}", subtitle,
        ),
        Paragraph("Executive summary", heading),
    ]
    metric_rows = _summary_rows(report)
    summary_table = Table(
        [[Paragraph(escape(metric_rows[i][0]), small), Paragraph(escape(metric_rows[i][1]), small),
          Paragraph(escape(metric_rows[i + 1][0]), small), Paragraph(escape(metric_rows[i + 1][1]), small)]
         for i in range(0, len(metric_rows), 2)],
        colWidths=[45 * mm, 40 * mm, 45 * mm, 40 * mm], hAlign="LEFT",
    )
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F7F7")),
        ("TEXTCOLOR", (0, 0), (0, -1), brand_color), ("TEXTCOLOR", (2, 0), (2, -1), brand_color),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D5DD")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([summary_table, Paragraph("Daily activity", heading)])
    daily_data = [["Date", "Questions", "Errors", "Refusals", "Helpful", "Not helpful"]]
    daily_data.extend([
        [row.day.isoformat(), str(row.questions), str(row.errors), str(row.refused), str(row.helpful), str(row.not_helpful)]
        for row in report.daily
    ])
    daily_table = Table(daily_data, repeatRows=1, hAlign="LEFT", colWidths=[35 * mm, 26 * mm, 26 * mm, 34 * mm, 26 * mm, 30 * mm])
    daily_table.setStyle(_pdf_table_style(brand_color, colors))
    story.append(daily_table if report.daily else Paragraph("No activity recorded in this period.", small))
    story.append(Paragraph("Provider usage", heading))
    provider_data = [["Provider", "Model", "Calls", "Median latency", "p95 latency"]]
    provider_data.extend([
        [row.provider, row.model or "—", str(row.calls), _display_ms(row.latency_p50_ms), _display_ms(row.latency_p95_ms)]
        for row in report.providers
    ])
    provider_table = Table(provider_data, repeatRows=1, hAlign="LEFT", colWidths=[42 * mm, 65 * mm, 26 * mm, 42 * mm, 42 * mm])
    provider_table.setStyle(_pdf_table_style(brand_color, colors))
    story.append(provider_table if report.providers else Paragraph("No provider usage recorded.", small))
    story.extend([Spacer(1, 8), Paragraph(escape(_provenance(report)), small)])

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D0D5DD"))
        canvas.line(16 * mm, 13 * mm, page_size[0] - 16 * mm, 13 * mm)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.setFont(font_name, 8)
        canvas.drawString(16 * mm, 8 * mm, _shape_rtl(_brand(profile))[:80])
        canvas.drawRightString(page_size[0] - 16 * mm, 8 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def _pdf_table_style(brand_color, colors):
    from reportlab.platypus import TableStyle

    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand_color),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D0D5DD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])


def _display_ms(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1f} ms"


def _render_docx(report: UsageReport, profile: CompanyProfile) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = section.right_margin = Inches(0.65)
    brand = RGBColor.from_string(profile.primary_color.lstrip("#").upper())
    title = document.add_heading(_brand(profile), 0)
    title.runs[0].font.color.rgb = brand
    document.add_heading("Usage and reliability report", level=1)
    document.add_paragraph(
        f"{report.period_start.date().isoformat()} to {report.period_end.date().isoformat()} | "
        f"Generated {report.generated_at.isoformat()}"
    )
    document.add_heading("Executive summary", level=2)
    summary = document.add_table(rows=0, cols=2)
    summary.style = "Light Shading Accent 1"
    for label, value in _summary_rows(report):
        cells = summary.add_row().cells
        cells[0].text, cells[1].text = label, value
    document.add_heading("Daily question volume", level=2)
    if report.daily:
        max_questions = max((row.questions for row in report.daily), default=0)
        activity_chart = document.add_table(rows=1, cols=3)
        activity_chart.style = "Light Shading Accent 1"
        for cell, label in zip(activity_chart.rows[0].cells, ("Date", "Relative volume", "Questions")):
            cell.text = label
        for row in report.daily:
            cells = activity_chart.add_row().cells
            cells[0].text = row.day.isoformat()
            blocks = round(row.questions / max_questions * 24) if max_questions else 0
            cells[1].text = "█" * blocks if blocks else "—"
            cells[2].text = str(row.questions)
            for run in cells[1].paragraphs[0].runs:
                run.font.name = "Arial"
                run.font.size = Pt(9)
                run.font.color.rgb = brand
        document.add_paragraph("Each bar is scaled to the largest daily question count in this report.")
    else:
        document.add_paragraph("No activity recorded; no chart was generated.")
    document.add_heading("Daily activity", level=2)
    daily = document.add_table(rows=1, cols=6)
    daily.style = "Light Shading Accent 1"
    for cell, label in zip(daily.rows[0].cells, ("Date", "Questions", "Errors", "Refusals", "Helpful", "Not helpful")):
        cell.text = label
    for row in report.daily:
        cells = daily.add_row().cells
        for cell, value in zip(cells, (row.day.isoformat(), row.questions, row.errors, row.refused, row.helpful, row.not_helpful)):
            cell.text = str(value)
    if not report.daily:
        document.add_paragraph("No activity recorded in this period.")
    document.add_heading("Provider usage", level=2)
    providers = document.add_table(rows=1, cols=5)
    providers.style = "Light Shading Accent 1"
    for cell, label in zip(providers.rows[0].cells, ("Provider", "Model", "Calls", "Median latency", "p95 latency")):
        cell.text = label
    for row in report.providers:
        cells = providers.add_row().cells
        for cell, value in zip(cells, (row.provider, row.model or "—", row.calls, _display_ms(row.latency_p50_ms), _display_ms(row.latency_p95_ms))):
            cell.text = str(value)
    if not report.providers:
        document.add_paragraph("No provider usage recorded.")
    footer = section.footer.paragraphs[0]
    footer.text = _provenance(report)
    for run in footer.runs:
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(102, 112, 133)
    _apply_docx_rtl(document)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _render_xlsx(report: UsageReport, profile: CompanyProfile) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    daily = workbook.create_sheet("Daily Activity")
    providers = workbook.create_sheet("Provider Usage")
    charts = workbook.create_sheet("Charts")
    brand = profile.primary_color.lstrip("#").upper()
    header_fill = PatternFill("solid", fgColor=brand)
    header_font = Font(color="FFFFFF", bold=True)
    title_font = Font(size=18, bold=True, color=brand)

    _append_xlsx(summary, [_brand(profile)])
    summary["A1"].font = title_font
    _append_xlsx(summary, ["Usage and reliability report"])
    _append_xlsx(summary, ["Period start", report.period_start.isoformat()])
    _append_xlsx(summary, ["Period end", report.period_end.isoformat()])
    _append_xlsx(summary, ["Generated at", report.generated_at.isoformat()])
    _append_xlsx(summary, [])
    _append_xlsx(summary, ["Metric", "Value"])
    for cell in summary[7]:
        cell.fill, cell.font = header_fill, header_font
    for label, value in _summary_rows(report):
        _append_xlsx(summary, [label, value])
    _append_xlsx(summary, [])
    _append_xlsx(summary, [_provenance(report)])
    summary.freeze_panes = "A8"
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 40

    _write_xlsx_table(daily, ["Date", "Questions", "Errors", "Refusals", "Helpful", "Not helpful"], [
        [row.day.isoformat(), row.questions, row.errors, row.refused, row.helpful, row.not_helpful] for row in report.daily
    ], header_fill, header_font, get_column_letter)
    _write_xlsx_table(providers, ["Provider", "Model", "Calls", "Median latency (ms)", "p95 latency (ms)"], [
        [row.provider, row.model or "", row.calls, row.latency_p50_ms, row.latency_p95_ms] for row in report.providers
    ], header_fill, header_font, get_column_letter)
    if report.daily:
        from openpyxl.chart import BarChart, LineChart, Reference

        charts["A1"] = "Editable charts sourced from the Daily Activity sheet"
        activity_chart = LineChart()
        activity_chart.title = "Daily questions, errors, and refusals"
        activity_chart.style = 13
        activity_chart.height, activity_chart.width = 8, 18
        activity_chart.y_axis.title, activity_chart.x_axis.title = "Events", "Date"
        activity_chart.add_data(
            Reference(daily, min_col=2, max_col=4, min_row=1, max_row=daily.max_row),
            titles_from_data=True,
        )
        activity_chart.set_categories(Reference(daily, min_col=1, min_row=2, max_row=daily.max_row))
        charts.add_chart(activity_chart, "A3")

        feedback_chart = BarChart()
        feedback_chart.type = "col"
        feedback_chart.style = 10
        feedback_chart.title = "Daily feedback"
        feedback_chart.y_axis.title, feedback_chart.x_axis.title = "Responses", "Date"
        feedback_chart.height, feedback_chart.width = 8, 18
        feedback_chart.add_data(
            Reference(daily, min_col=5, max_col=6, min_row=1, max_row=daily.max_row),
            titles_from_data=True,
        )
        feedback_chart.set_categories(Reference(daily, min_col=1, min_row=2, max_row=daily.max_row))
        charts.add_chart(feedback_chart, "A20")
    else:
        charts["A1"] = "No daily activity; charts are omitted until source data is available."
    if _contains_rtl(f"{profile.company_name} {profile.brand_name}"):
        for sheet in workbook.worksheets:
            sheet.sheet_view.rightToLeft = True
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _write_xlsx_table(sheet, headers, rows, header_fill, header_font, get_column_letter) -> None:
    from openpyxl.styles import Alignment

    _append_xlsx(sheet, headers)
    for cell in sheet[1]:
        cell.fill, cell.font = header_fill, header_font
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        _append_xlsx(sheet, row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, column in enumerate(sheet.columns, start=1):
        values = [len(str(cell.value or "")) for cell in column]
        sheet.column_dimensions[get_column_letter(index)].width = min(max(max(values, default=10) + 2, 12), 42)


def _append_xlsx(sheet, values) -> None:
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            # openpyxl marks strings beginning with '=' as formulas; force all supplied text to string cells.
            cell.data_type = "s"


def _render_pptx(report: UsageReport, profile: CompanyProfile) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    brand = RGBColor.from_string(profile.primary_color.lstrip("#").upper())
    metrics = _summary_rows(report)
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    _pptx_header(title_slide, _brand(profile), brand, Inches, Pt)
    title = title_slide.shapes.add_textbox(Inches(0.8), Inches(1.65), Inches(11.6), Inches(0.75)).text_frame
    title.text = "Usage and reliability report"
    title.paragraphs[0].font.size = Pt(30)
    title.paragraphs[0].font.bold = True
    title.paragraphs[0].font.color.rgb = brand
    subtitle = title_slide.shapes.add_textbox(Inches(0.85), Inches(2.55), Inches(11.3), Inches(0.9)).text_frame
    subtitle.text = (
        f"{report.period_start.date().isoformat()} to {report.period_end.date().isoformat()}\n"
        f"Generated {report.generated_at.isoformat()}"
    )
    subtitle.paragraphs[0].font.size = Pt(18)
    subtitle.paragraphs[1].font.size = Pt(12)
    note = title_slide.shapes.add_textbox(Inches(0.85), Inches(4.35), Inches(11.3), Inches(1.0)).text_frame
    note.word_wrap = True
    note.vertical_anchor = MSO_ANCHOR.MIDDLE
    note.text = (
        "Scope: aggregate RAG trace and feedback metadata only.\n"
        "Question text, answer text, usernames, and document content are excluded."
    )
    for paragraph in note.paragraphs:
        paragraph.font.size = Pt(12)
        paragraph.font.color.rgb = RGBColor(52, 64, 84)
    _pptx_footer(title_slide, report, Inches, Pt)

    summary_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    _pptx_header(summary_slide, "Executive summary", brand, Inches, Pt)
    for index, (label, value) in enumerate(metrics):
        column, row = index % 3, index // 3
        left, top = Inches(0.75 + column * 4.15), Inches(1.35 + row * 1.45)
        shape = summary_slide.shapes.add_shape(1, left, top, Inches(3.65), Inches(1.12))
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(244, 247, 247)
        shape.line.color.rgb = RGBColor(208, 213, 221)
        frame = shape.text_frame
        frame.clear()
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = frame.paragraphs[0]
        p.text = label
        p.font.size, p.font.color.rgb = Pt(12), brand
        p.alignment = PP_ALIGN.CENTER
        p = frame.add_paragraph()
        p.text = value
        p.font.size, p.font.bold = Pt(22), True
        p.font.color.rgb = RGBColor(52, 64, 84)
        p.alignment = PP_ALIGN.CENTER
    _pptx_footer(summary_slide, report, Inches, Pt)

    daily_rows = [[row.day.isoformat(), str(row.questions), str(row.errors), str(row.refused), str(row.helpful), str(row.not_helpful)] for row in report.daily]
    _pptx_table_slides(presentation, "Daily activity", ["Date", "Questions", "Errors", "Refusals", "Helpful", "Not helpful"], daily_rows or [["No activity", "—", "—", "—", "—", "—"]], brand, report, Inches, Pt)
    provider_rows = [[row.provider, row.model or "—", str(row.calls), _display_ms(row.latency_p50_ms), _display_ms(row.latency_p95_ms)] for row in report.providers]
    _pptx_table_slides(presentation, "Provider usage", ["Provider", "Model", "Calls", "Median latency", "p95 latency"], provider_rows or [["No provider usage", "—", "—", "—", "—"]], brand, report, Inches, Pt)
    if report.daily:
        _pptx_activity_chart_slide(
            presentation,
            "Daily questions, errors, and refusals",
            report,
            [("Questions", "questions", "146B61"), ("Errors", "errors", "D92D20"), ("Refusals", "refused", "B54708")],
            brand,
            Inches,
            Pt,
        )
        _pptx_activity_chart_slide(
            presentation,
            "Daily feedback",
            report,
            [("Helpful", "helpful", "146B61"), ("Not helpful", "not_helpful", "D92D20")],
            brand,
            Inches,
            Pt,
        )
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def _pptx_activity_chart_slide(presentation, title, report, series_specs, brand, Inches, Pt) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_MARKER_STYLE

    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    _pptx_header(slide, title, brand, Inches, Pt)
    data = CategoryChartData()
    data.categories = [row.day.isoformat() for row in report.daily]
    for label, field, _color in series_specs:
        data.add_series(label, [getattr(row, field) for row in report.daily])
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.LINE_MARKERS,
        Inches(0.85),
        Inches(1.25),
        Inches(11.7),
        Inches(5.5),
        data,
    ).chart
    chart.has_legend = len(series_specs) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.value_axis.minimum_scale = 0
    chart.category_axis.tick_labels.font.size = Pt(9)
    chart.value_axis.tick_labels.font.size = Pt(9)
    for series, (_label, _field, color) in zip(chart.series, series_specs):
        series.format.line.color.rgb = RGBColor.from_string(color)
        series.format.line.width = Pt(2)
        series.marker.style = XL_MARKER_STYLE.CIRCLE
        series.marker.size = 5


def _pptx_header(slide, heading: str, brand, Inches, Pt) -> None:
    from pptx.enum.text import MSO_ANCHOR

    shape = slide.shapes.add_textbox(Inches(0.75), Inches(0.35), Inches(11.8), Inches(0.58))
    frame = shape.text_frame
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    frame.text = heading
    p = frame.paragraphs[0]
    _pptx_apply_rtl(p)
    p.font.size, p.font.bold, p.font.color.rgb = Pt(24), True, brand


def _pptx_footer(slide, report: UsageReport, Inches, Pt) -> None:
    from pptx.dml.color import RGBColor

    shape = slide.shapes.add_textbox(Inches(0.75), Inches(7.08), Inches(11.8), Inches(0.24))
    p = shape.text_frame.paragraphs[0]
    p.text = f"InfraSpine AI | {report.period_start.date().isoformat()} to {report.period_end.date().isoformat()} | Aggregate metadata only"
    p.font.size, p.font.color.rgb = Pt(8), RGBColor(102, 112, 133)


def _pptx_table_slides(presentation, title, headers, rows, brand, report, Inches, Pt) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.util import Inches as InchesType

    per_slide = 17
    for offset in range(0, len(rows), per_slide):
        batch = rows[offset: offset + per_slide]
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        heading = title if len(rows) <= per_slide else f"{title} ({offset + 1}–{offset + len(batch)})"
        _pptx_header(slide, heading, brand, Inches, Pt)
        header_height = 0.55
        body_row_height = min(0.42, (5.45 - header_height) / len(batch))
        table_height = header_height + body_row_height * len(batch)
        table_shape = slide.shapes.add_table(
            len(batch) + 1, len(headers), Inches(0.7), Inches(1.2), Inches(11.95), InchesType(table_height)
        )
        table = table_shape.table
        table.first_row = True
        table.rows[0].height = InchesType(header_height)
        for row_index in range(1, len(table.rows)):
            table.rows[row_index].height = InchesType(body_row_height)
        widths = [1.7, 2.4, 1.2, 2.3, 2.3, 2.0] if len(headers) == 6 else [2.0, 3.0, 1.4, 2.6, 2.6]
        for col, label in enumerate(headers):
            cell = table.cell(0, col)
            cell.text = label
            for paragraph in cell.text_frame.paragraphs:
                _pptx_apply_rtl(paragraph)
            cell.fill.solid()
            cell.fill.fore_color.rgb = brand
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_top = cell.margin_bottom = Pt(1)
            for p in cell.text_frame.paragraphs:
                p.font.size, p.font.bold, p.font.color.rgb = Pt(12), True, RGBColor(255, 255, 255)
                p.alignment = PP_ALIGN.CENTER
            table.columns[col].width = Inches(widths[col])
        for row_index, values in enumerate(batch, start=1):
            for col, value in enumerate(values):
                cell = table.cell(row_index, col)
                cell.text = str(value)
                for paragraph in cell.text_frame.paragraphs:
                    _pptx_apply_rtl(paragraph)
                if row_index % 2 == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor(248, 250, 252)
                else:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor(255, 255, 255)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_top = cell.margin_bottom = Pt(1)
                for p in cell.text_frame.paragraphs:
                    p.font.size = Pt(11)
                    p.font.color.rgb = RGBColor(52, 64, 84)
        _pptx_footer(slide, report, Inches, Pt)


def _pptx_apply_rtl(paragraph) -> None:
    if not _contains_rtl(paragraph.text):
        return
    from pptx.enum.text import PP_ALIGN

    properties = paragraph._p.get_or_add_pPr()
    properties.set("rtl", "1")
    paragraph.alignment = PP_ALIGN.RIGHT
    for run in paragraph.runs:
        run._r.get_or_add_rPr().set("lang", "ar-SA")
