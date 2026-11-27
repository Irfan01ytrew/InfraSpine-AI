import csv
from datetime import UTC, date, datetime
from io import BytesIO, StringIO

import pytest

from app.schemas.deployment import CompanyProfile
from app.schemas.reports import DailyUsage, ProviderUsage, UsageReport
from app.schemas.traces import TraceSummary
from app.services.report_export import render_usage_export


@pytest.fixture
def sample_report() -> UsageReport:
    return UsageReport(
        generated_at=datetime(2026, 10, 22, 12, tzinfo=UTC),
        period_start=datetime(2026, 10, 1, tzinfo=UTC),
        period_end=datetime(2026, 10, 22, 12, tzinfo=UTC),
        summary=TraceSummary(
            days=21, questions=12, errors=1, refused=2, general=3, latency_p50_ms=122.4,
            latency_p95_ms=355.8, llm_calls=11, prompt_tokens=2100, completion_tokens=875,
            helpful=7, not_helpful=2, decisions=[],
        ),
        daily=[DailyUsage(day=date(2026, 10, 21), questions=12, errors=1, refused=2, helpful=7, not_helpful=2)],
        providers=[ProviderUsage(provider="gemini", model="gemini-test", calls=11, latency_p50_ms=122.4, latency_p95_ms=355.8)],
    )


@pytest.fixture
def company_profile() -> CompanyProfile:
    return CompanyProfile(
        company_name="North & West Engineering",
        industry="Infrastructure",
        brand_name="North & West",
        primary_color="#146B61",
        setup_complete=True,
    )


@pytest.mark.parametrize("format,media_type,extension", [
    ("csv", "text/csv; charset=utf-8", b".csv"),
    ("pdf", "application/pdf", b".pdf"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b".docx"),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b".xlsx"),
    ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", b".pptx"),
])
def test_exports_are_real_branded_editable_files(sample_report, company_profile, format, media_type, extension):
    content, actual_media_type, filename = render_usage_export(sample_report, company_profile, format)
    assert content and actual_media_type == media_type
    assert filename.endswith(extension.decode())

    if format == "csv":
        assert content.startswith(b"\xef\xbb\xbf")
        assert b"North & West" in content and b"Prompt tokens" in content and b"Provider usage" in content
    elif format == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        assert len(reader.pages) >= 1
        text = " ".join(page.extract_text() or "" for page in reader.pages)
        assert "North & West" in text and "Daily activity" in text and "gemini-test" in text
    elif format == "docx":
        from docx import Document

        document = Document(BytesIO(content))
        text = " ".join(paragraph.text for paragraph in document.paragraphs)
        assert "North & West" in text and "Usage and reliability report" in text
        assert len(document.tables) == 4
        assert document.tables[1].rows[1].cells[0].text == "2026-10-21"
        assert document.tables[1].rows[1].cells[1].text == "█" * 24
        assert document.tables[1].rows[1].cells[2].text == "12"
        assert str(document.paragraphs[0].runs[0].font.color.rgb) == "146B61"
    elif format == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(content), data_only=True)
        assert workbook.sheetnames == ["Summary", "Daily Activity", "Provider Usage", "Charts"]
        assert workbook["Daily Activity"].max_row == 2
        assert workbook["Provider Usage"]["A2"].value == "gemini"
        assert workbook["Summary"]["A1"].font.color.rgb.endswith("146B61")
        assert workbook["Daily Activity"]["A1"].fill.fgColor.rgb.endswith("146B61")
        assert len(workbook["Charts"]._charts) == 2
        assert len(workbook["Charts"]._charts[0].series) == 3
        assert "Daily Activity" in workbook["Charts"]._charts[0].series[0].val.numRef.f
    else:
        from pptx import Presentation
        from pptx.dml.color import RGBColor

        presentation = Presentation(BytesIO(content))
        assert len(presentation.slides) >= 4
        text = " ".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
        table_text = " ".join(
            cell.text
            for slide in presentation.slides
            for shape in slide.shapes
            if shape.has_table
            for row in shape.table.rows
            for cell in row.cells
        )
        assert "North & West" in text and "Executive summary" in text and "gemini-test" in table_text
        brand_header = next(shape for shape in presentation.slides[0].shapes if shape.has_text_frame and shape.text == "North & West")
        assert str(brand_header.text_frame.paragraphs[0].font.color.rgb) == "146B61"
        scope_note = next(shape for shape in presentation.slides[0].shapes if shape.has_text_frame and shape.text.startswith("Scope:"))
        assert scope_note.text_frame.word_wrap is True
        assert "usernames" in scope_note.text
        assert all(paragraph.font.size.pt == 12 for paragraph in scope_note.text_frame.paragraphs)
        assert all(paragraph.font.color.rgb == RGBColor(52, 64, 84) for paragraph in scope_note.text_frame.paragraphs)

        summary_cards = [
            shape for shape in presentation.slides[1].shapes
            if shape.has_text_frame and len(shape.text_frame.paragraphs) == 2
        ]
        assert len(summary_cards) == 12
        assert all(shape.text_frame.paragraphs[1].font.color.rgb == RGBColor(52, 64, 84) for shape in summary_cards)
        table_shapes = [shape for slide in presentation.slides for shape in slide.shapes if shape.has_table]
        chart_shapes = [shape for slide in presentation.slides for shape in slide.shapes if shape.has_chart]
        assert len(chart_shapes) == 2
        assert [series.name for series in chart_shapes[0].chart.series] == ["Questions", "Errors", "Refusals"]
        assert [series.name for series in chart_shapes[1].chart.series] == ["Helpful", "Not helpful"]
        for table_shape in table_shapes:
            table = table_shape.table
            assert table_shape.height.inches <= 1.2  # Synthetic report has one row; it must not stretch across the slide.
            assert all(
                table.cell(row_index, column_index).text_frame.paragraphs[0].font.color.rgb == RGBColor(52, 64, 84)
                for row_index in range(1, len(table.rows))
                for column_index in range(len(table.columns))
            )


def test_office_exports_omit_visualizations_when_daily_source_is_empty(sample_report, company_profile):
    report = sample_report.model_copy(update={"daily": []})

    docx_content, _, _ = render_usage_export(report, company_profile, "docx")
    from docx import Document

    document = Document(BytesIO(docx_content))
    assert "No activity recorded; no chart was generated." in [p.text for p in document.paragraphs]
    assert len(document.tables) == 3

    xlsx_content, _, _ = render_usage_export(report, company_profile, "xlsx")
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(xlsx_content), data_only=False)
    assert workbook["Charts"]["A1"].value.startswith("No daily activity")
    assert workbook["Charts"]._charts == []

    pptx_content, _, _ = render_usage_export(report, company_profile, "pptx")
    from pptx import Presentation

    presentation = Presentation(BytesIO(pptx_content))
    assert not any(shape.has_chart for slide in presentation.slides for shape in slide.shapes)


def test_export_rejects_unknown_formats(sample_report, company_profile):
    with pytest.raises(KeyError):
        render_usage_export(sample_report, company_profile, "html")


def test_csv_and_xlsx_keep_untrusted_labels_as_text(sample_report, company_profile):
    malicious_model = "=HYPERLINK(\"https://example.invalid\",\"open\")"
    report = sample_report.model_copy(update={
        "providers": [ProviderUsage(
            provider="@SUM(1,1)", model=malicious_model, calls=1, latency_p50_ms=1.0, latency_p95_ms=2.0,
        )],
    })
    csv_content, _, _ = render_usage_export(report, company_profile.model_copy(update={"brand_name": "=1+1"}), "csv")
    csv_rows = list(csv.reader(StringIO(csv_content.decode("utf-8-sig"))))
    assert csv_rows[0][0] == "'=1+1"
    provider_index = next(i for i, row in enumerate(csv_rows) if row and row[0] == "Provider")
    assert csv_rows[provider_index + 1][0] == "'@SUM(1,1)"
    assert csv_rows[provider_index + 1][1] == "'" + malicious_model

    xlsx_content, _, _ = render_usage_export(report, company_profile.model_copy(update={"brand_name": "=1+1"}), "xlsx")
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(xlsx_content), data_only=False)
    assert workbook["Summary"]["A1"].value == "=1+1"
    assert workbook["Summary"]["A1"].data_type == "s"
    assert workbook["Provider Usage"]["A2"].value == "@SUM(1,1)"
    assert workbook["Provider Usage"]["A2"].data_type == "s"
    assert workbook["Provider Usage"]["B2"].value == malicious_model
    assert workbook["Provider Usage"]["B2"].data_type == "s"


def test_arabic_pdf_report_renders_with_unicode_font(sample_report):
    arabic_profile = CompanyProfile(
        company_name="شركة نورث ويست للهندسة",
        industry="البنية التحتية",
        brand_name="نورث ويست",
        primary_color="#146B61",
        setup_complete=True,
    )
    content, media_type, filename = render_usage_export(sample_report, arabic_profile, "pdf")
    assert content and media_type == "application/pdf"
    assert content.startswith(b"%PDF-")

    from pypdf import PdfReader
    reader = PdfReader(BytesIO(content))
    assert len(reader.pages) >= 1

    from zipfile import ZipFile

    docx_content, _, _ = render_usage_export(sample_report, arabic_profile, "docx")
    with ZipFile(BytesIO(docx_content)) as archive:
        word_xml = archive.read("word/document.xml")
    assert "نورث ويست".encode() in word_xml
    assert b"w:bidi" in word_xml and b"w:rtl" in word_xml

    xlsx_content, _, _ = render_usage_export(sample_report, arabic_profile, "xlsx")
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(xlsx_content), data_only=False)
    assert all(sheet.sheet_view.rightToLeft for sheet in workbook.worksheets)

    pptx_content, _, _ = render_usage_export(sample_report, arabic_profile, "pptx")
    with ZipFile(BytesIO(pptx_content)) as archive:
        slide_xml = archive.read("ppt/slides/slide1.xml")
    assert "نورث ويست".encode() in slide_xml
    assert b'rtl="1"' in slide_xml
