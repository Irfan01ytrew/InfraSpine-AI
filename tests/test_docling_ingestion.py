"""Docling runs inside the ingestion worker for PDF, DOCX and PPTX, and the built-in parsers remain the fallback."""

import sys

import pytest

from app.main import app
from app.rag.parsers.registry import PARSERS
from tests.conftest import USER_EMAIL, login
from tests.rag_helpers import make_pdf
from tests.test_document_formats import chunks, make_docx, make_pptx, ready


def _forbid_fallback(monkeypatch, file_format: str) -> None:
    def used(*_args, **_kwargs):
        raise AssertionError(f"built-in {file_format} parser ran")

    monkeypatch.setattr(PARSERS[file_format], "parse", used)


def test_worker_extracts_docx_and_pptx_with_docling(client, monkeypatch, tmp_path):
    marker = tmp_path / "engine.txt"
    monkeypatch.setenv("INFRASPINE_DOCLING_MARKER", str(marker))
    _forbid_fallback(monkeypatch, "docx")
    _forbid_fallback(monkeypatch, "pptx")
    login(client, USER_EMAIL)

    doc = ready(client, "handbook.docx", make_docx())
    assert marker.read_text(encoding="utf-8") == "docling"
    rows = chunks(doc["id"])
    by_label = {row.locator["label"]: row for row in rows}
    assert list(by_label) == ["Document start", "Remote access", "Remote access > Laptops · Table 1, row 1", "Printing"]
    laptops = by_label["Remote access > Laptops · Table 1, row 1"]  # the table as whole rows with column names
    assert laptops.locator["heading_path"] == ["Remote access", "Laptops"]
    assert "Row 1: Model: Contoso Book 14; Refresh cycle: four years" in laptops.content

    deck = ready(client, "review.pptx", make_pptx())
    assert marker.read_text(encoding="utf-8") == "docling"
    slides = chunks(deck["id"])
    assert [row.locator["label"] for row in slides] == [
        "Slide 1 – Quarterly review",
        "Slide 2 – Datacenter cooling",
        "Slide 3 – Support hours",
    ]
    assert "Speaker notes: Mention the summer peak plan." in slides[1].content
    assert "EMEA | 07:00 to 19:00" in slides[2].content
    assert [row.page_number for row in slides] == [1, 2, 3]


@pytest.mark.skipif(sys.platform == "win32", reason="Native Docling is disabled on Windows; use the Linux container parser path.")
def test_worker_extracts_pdf_pages_with_docling(client, monkeypatch, tmp_path):
    import time

    import psutil

    marker = tmp_path / "engine.txt"
    monkeypatch.setenv("INFRASPINE_DOCLING_MARKER", str(marker))
    _forbid_fallback(monkeypatch, "pdf")
    login(client, USER_EMAIL)
    process = psutil.Process()
    before = process.memory_info().rss
    started = time.perf_counter()
    doc = ready(client, "handbook.pdf", make_pdf("Cover page.", "Refund requests are accepted within 30 days."))
    elapsed = time.perf_counter() - started
    after = process.memory_info().rss
    print(
        f"DOCLING_PDF_WORKER seconds={elapsed:.1f} rss_before_mb={before / 2**20:.0f} "
        f"rss_after_mb={after / 2**20:.0f}"
    )
    assert marker.read_text(encoding="utf-8") == "docling"
    assert [row.locator for row in chunks(doc["id"])] == [
        {"kind": "page", "position": 1, "label": "Page 1", "page": 1},
        {"kind": "page", "position": 2, "label": "Page 2", "page": 2},
    ]
    text = " ".join(row.content for row in chunks(doc["id"]))
    assert "Cover page." in text
    assert "Refund requests are accepted within 30 days." in text


def test_isolated_worker_child_uses_docling(client, monkeypatch, tmp_path):
    marker = tmp_path / "engine.txt"
    monkeypatch.setenv("INFRASPINE_DOCLING_MARKER", str(marker))
    monkeypatch.setattr(app.state.rag.worker, "isolate", True)
    login(client, USER_EMAIL)
    doc = ready(client, "handbook.docx", make_docx())
    assert marker.read_text(encoding="utf-8") == "docling"
    assert chunks(doc["id"])[1].locator["label"] == "Remote access"


def test_docling_failure_uses_builtin_parser(client, monkeypatch, tmp_path):
    marker = tmp_path / "engine.txt"
    monkeypatch.setenv("INFRASPINE_DOCLING_MARKER", str(marker))

    def failed(*_args, **_kwargs):
        from app.rag.parsers.docling_extract import DoclingFailed

        raise DoclingFailed("forced")

    monkeypatch.setattr("app.rag.parsers.docling_extract.extract_with_docling", failed)
    login(client, USER_EMAIL)
    doc = ready(client, "handbook.docx", make_docx())
    assert marker.read_text(encoding="utf-8") == "fallback"
    assert [row.locator["label"] for row in chunks(doc["id"])] == [
        "Document start",
        "Remote access",
        "Remote access > Laptops",
        "Printing",
    ]


def test_partial_docling_conversion_uses_builtin_parser(client, monkeypatch, tmp_path):
    """Docling reporting partial success (some pages or elements dropped) must not index an incomplete document."""
    from types import SimpleNamespace

    from app.rag.parsers import docling_extract

    marker = tmp_path / "engine.txt"
    monkeypatch.setenv("INFRASPINE_DOCLING_MARKER", str(marker))
    converter = docling_extract._get_converter()
    real_convert = converter.convert

    def partial(path):
        result = real_convert(path)
        return SimpleNamespace(status=SimpleNamespace(value="partial_success"), errors=["page 2 failed"],
                               document=result.document)

    monkeypatch.setattr(converter, "convert", partial)
    login(client, USER_EMAIL)
    doc = ready(client, "handbook.docx", make_docx())
    assert marker.read_text(encoding="utf-8") == "fallback"
    assert [row.locator["label"] for row in chunks(doc["id"])] == [
        "Document start",
        "Remote access",
        "Remote access > Laptops",
        "Printing",
    ]


def test_retry_after_a_parser_crash_uses_the_builtin_parser(client, monkeypatch):
    """A native Docling crash kills the parser process before its own fallback can run. Retrying with Docling
    would crash the same way on every attempt, so the retry goes straight to the built-in parser."""
    from sqlalchemy import text

    from tests.conftest import run_db
    from tests.rag_helpers import document, process_all, upload

    def docling_must_not_run(*_args, **_kwargs):
        raise AssertionError("Docling ran again after a crash")

    monkeypatch.setattr("app.rag.parsers.docling_extract.extract_with_docling", docling_must_not_run)
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.docx", make_docx(), content_type="application/octet-stream").json()
    # What the worker records when an attempt's parser process dies (see IngestionWorker.process_next).
    run_db(lambda c: c.execute(
        text("UPDATE documents SET attempts = 1, error_code = 'processing_crashed' WHERE id = :id"), {"id": doc["id"]}
    ))
    process_all(client)
    assert document(client, doc["id"])["status"] == "ready"
    assert [row.locator["label"] for row in chunks(doc["id"])] == [
        "Document start",
        "Remote access",
        "Remote access > Laptops",
        "Printing",
    ]


def test_docling_pdf_conversion_is_time_boxed_inside_the_processing_timeout():
    """A Docling parse that would outlive the processing timeout must stop early (partial success, then the
    built-in parser) instead of being killed with nothing indexed."""
    from docling.datamodel.base_models import InputFormat

    from app.core.config import get_settings
    from app.rag.parsers.docling_extract import _get_converter

    budget = _get_converter().format_to_options[InputFormat.PDF].pipeline_options.document_timeout
    assert 0 < budget < get_settings().rag_processing_timeout_seconds
    assert _get_converter().format_to_options[InputFormat.PDF].pipeline_options.do_ocr is False


def test_docling_mixed_pdf_uses_local_ocr_for_only_blank_pages(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.rag.parsers.base import ParseLimits, Section, page_locator
    from app.rag.parsers import docling_extract

    page_one_text = SimpleNamespace(
        label=SimpleNamespace(value="text"),
        text="Selectable PDF text.",
        prov=[SimpleNamespace(page_no=1)],
    )

    class FakeDocument:
        pages = {1: object(), 2: object()}

        def iterate_items(self, **_kwargs):
            return iter([(page_one_text, None)])

    class FakeConverter:
        def convert(self, _path):
            return SimpleNamespace(
                status=SimpleNamespace(value="success"), errors=[], document=FakeDocument()
            )

    ocr_pages = []

    def local_ocr(_path, _limits, pages):
        ocr_pages.extend(pages)
        return [Section("Scanned page text.", page_locator(page)) for page in pages]

    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(rag_ocr_enabled=True))
    monkeypatch.setattr(docling_extract, "_get_converter", lambda pictures=False: FakeConverter())
    monkeypatch.setattr("app.rag.ocr.ocr_pdf_pages", local_ocr)
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"synthetic placeholder; converter is stubbed")
    limits = ParseLimits(max_units=50, max_chars=100_000, max_unpacked_bytes=20_000_000, section_chars=800)

    sections = docling_extract.extract_with_docling(path, "pdf", limits)

    assert ocr_pages == [2]
    assert [(section.locator.position, section.text) for section in sections] == [
        (1, "Selectable PDF text."),
        (2, "Scanned page text."),
    ]


def test_docling_recovers_digital_text_before_ocr_for_pages_it_missed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.rag.parsers.base import ParseLimits, Section, page_locator
    from app.rag.parsers import docling_extract

    page_one_text = SimpleNamespace(
        label=SimpleNamespace(value="text"), text="Selectable PDF text.", prov=[SimpleNamespace(page_no=1)]
    )

    class FakeDocument:
        pages = {1: object(), 2: object()}

        def iterate_items(self, **_kwargs):
            return iter([(page_one_text, None)])

    class FakeConverter:
        def convert(self, _path):
            return SimpleNamespace(status=SimpleNamespace(value="success"), errors=[], document=FakeDocument())

    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(rag_ocr_enabled=True))
    monkeypatch.setattr(docling_extract, "_get_converter", lambda pictures=False: FakeConverter())
    monkeypatch.setattr(
        "app.rag.pdf.extract_pages",
        lambda *_args, **_kwargs: [
            SimpleNamespace(number=1, text="Selectable PDF text."),
            SimpleNamespace(number=2, text="Small digital footer recovered by the built-in parser."),
        ],
    )

    def unexpected_ocr(*_args, **_kwargs):
        raise AssertionError("OCR should not run after built-in PDF text was recovered")

    monkeypatch.setattr("app.rag.ocr.ocr_pdf_pages", unexpected_ocr)
    path = tmp_path / "missed-footer.pdf"
    path.write_bytes(b"synthetic placeholder; converter is stubbed")
    limits = ParseLimits(max_units=50, max_chars=100_000, max_unpacked_bytes=20_000_000, section_chars=800)

    sections = docling_extract.extract_with_docling(path, "pdf", limits)

    assert [(section.locator.position, section.text) for section in sections] == [
        (1, "Selectable PDF text."),
        (2, "Small digital footer recovered by the built-in parser."),
    ]


def test_local_ocr_ignores_truly_blank_pages_but_rejects_unreadable_scans(monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    import pypdfium2

    from app.rag.ocr import ocr_pdf_pages
    from app.rag.parsers.base import ParseError, ParseLimits

    class Bitmap:
        def __init__(self, pixels):
            self.pixels = pixels

        def to_numpy(self):
            return self.pixels

    class Page:
        def __init__(self, pixels):
            self.pixels = pixels

        def get_size(self):
            return (100, 100)

        def render(self, **_kwargs):
            return Bitmap(self.pixels)

    class Pdf:
        def __init__(self, pixels):
            self.pages = [Page(pixels)]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            pass

    pixels = np.full((100, 100, 3), 255, dtype=np.uint8)
    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda _path: Pdf(pixels))
    monkeypatch.setattr("app.rag.ocr._local_engine", lambda: lambda _image: SimpleNamespace(txts=[]))
    limits = ParseLimits(max_units=10, max_chars=10_000, max_unpacked_bytes=1_000_000, section_chars=800)

    [blank] = ocr_pdf_pages("blank.pdf", limits, [1])
    assert blank.text == ""

    pixels[50:60, 20:40] = 0  # visible scanned content that OCR failed to read
    with pytest.raises(ParseError, match="nonblank PDF page") as error:
        ocr_pdf_pages("unreadable-scan.pdf", limits, [1])
    assert error.value.code == "ocr_no_text"


def test_native_windows_routes_pdf_to_builtin_parser(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.rag.parsers.base import ParseLimits
    from app.rag.parsers.registry import docling_runtime_enabled, parse_document

    monkeypatch.setattr(sys, "platform", "win32")
    settings = SimpleNamespace(rag_use_docling=True, rag_ocr_enabled=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    def docling_must_not_run(*_args, **_kwargs):
        raise AssertionError("native Docling must remain disabled on Windows")

    monkeypatch.setattr("app.rag.parsers.docling_extract.extract_with_docling", docling_must_not_run)
    from tests.rag_helpers import make_pdf

    path = tmp_path / "safe.pdf"
    path.write_bytes(make_pdf("Synthetic Windows fallback page."))
    limits = ParseLimits(max_units=50, max_chars=100_000, max_unpacked_bytes=20_000_000, section_chars=800)

    sections = parse_document(path, "pdf", limits, use_docling=True)

    assert sections
    assert "Synthetic Windows fallback page." in " ".join(section.text for section in sections)
    assert docling_runtime_enabled(settings, "docx") is True


def test_converter_is_reused_for_a_second_document(tmp_path):
    from app.rag.parsers.base import ParseLimits
    from app.rag.parsers.docling_extract import _get_converter, extract_with_docling

    first = tmp_path / "a.docx"
    second = tmp_path / "b.docx"
    payload = make_docx()
    first.write_bytes(payload)
    second.write_bytes(payload)
    limits = ParseLimits(max_units=50, max_chars=100_000, max_unpacked_bytes=20_000_000, section_chars=800)
    extract_with_docling(first, "docx", limits)
    created = _get_converter()
    extract_with_docling(second, "docx", limits)
    assert _get_converter() is created


# ---------------------------------------------------------------- image understanding (pictures in PDFs)

CHART_DESCRIPTION = (
    "Bar chart titled Quarterly revenue (PKR million). Q1 2.1, Q2 3.4, Q3 4.2, Q4 3.8. Q3 has the highest revenue."
)


@pytest.mark.skipif(sys.platform == "win32", reason="Native Docling is disabled on Windows; use the Linux container parser path.")
def test_pdf_chart_is_described_indexed_and_cited_by_page_under_its_acl(client, fake_provider):
    from tests.conftest import ADMIN_EMAIL, USER2_EMAIL
    from tests.rag_helpers import activate_provider, allow_images, ask, make_pdf_with_chart, process_all, upload
    from tests.test_access_control import set_access

    fake_provider.vision_reply = CHART_DESCRIPTION
    fake_provider.rag_reply = lambda message: "Q3 had the highest revenue, 4.2 million [S1]."
    activate_provider(client)  # signs in as the admin
    assert allow_images(client).status_code == 200  # the only switch: no environment setting is needed
    doc = upload(client, "annual-report.pdf", make_pdf_with_chart("Annual report introduction.", "Revenue for the year.")).json()
    assert set_access(client, doc["id"], "restricted", users=[USER_EMAIL]).status_code == 200
    process_all(client)

    rows = chunks(doc["id"])
    pictures = [row for row in rows if row.content.startswith("Image on page")]
    assert len(fake_provider.image_calls) == 1 and fake_provider.image_calls[0].startswith(b"\x89PNG")
    assert len(pictures) == 1 and pictures[0].page_number == 2 and pictures[0].locator["kind"] == "page"
    assert "Q3 4.2" in pictures[0].content
    assert pictures[0].content.startswith("Image on page 2: ")
    assert "Quarterly revenue (PKR million)" in pictures[0].content  # the provider description keeps the chart title

    login(client, USER_EMAIL)
    for question in ("Which quarter had the highest revenue?", "What does the chart on page 2 show?"):
        body = ask(client, question).json()  # a question about the chart is answered from the document
        assert body.get("chart") is None, question
        assert [(c["filename"], c["page_number"]) for c in body["citations"]] == [("annual-report.pdf", 2)], question

    question = "Which quarter had the highest revenue?"
    login(client, USER2_EMAIL)  # no grant: neither the description nor the page image is reachable
    assert not ask(client, question).json()["citations"]
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/2/image").status_code == 404

    login(client, ADMIN_EMAIL)
    assert set_access(client, doc["id"], "restricted", users=[USER2_EMAIL]).status_code == 200
    login(client, USER_EMAIL)  # revoked: the next question no longer finds it
    assert not ask(client, question).json()["citations"]


@pytest.mark.skipif(sys.platform == "win32", reason="Native Docling is disabled on Windows; use the Linux container parser path.")
def test_pictures_are_sent_only_with_authorization_to_a_vision_model(client, fake_provider):
    from app.core.errors import LLMTimeoutError
    from tests.conftest import ADMIN_EMAIL
    from tests.rag_helpers import PROVIDER, activate_provider, allow_images, make_pdf_with_chart, process_all, upload

    def described(doc_id: str) -> list:
        rows = chunks(doc_id)
        assert rows  # the text is always indexed
        return [row for row in rows if row.content.startswith("Image on page")]

    fake_provider.vision_reply = CHART_DESCRIPTION
    activate_provider(client)  # signs in as the admin
    status = client.get("/api/rag/image-understanding").json()
    assert status["state"] == "not_authorized" and not status["available"]
    first = upload(client, "unauthorized.pdf", make_pdf_with_chart("Plain introduction.", "Chart page text.")).json()
    process_all(client)
    assert not described(first["id"]) and fake_provider.image_calls == []  # nothing left the server

    login(client, USER_EMAIL)  # only an admin can authorize
    attempt = client.put(f"{PROVIDER}/image-sharing", json={"authorized": True, "expected_version": 1})
    assert attempt.status_code == 403
    login(client, ADMIN_EMAIL)
    assert allow_images(client).status_code == 200
    state = client.get(PROVIDER).json()
    assert state["image_sharing_authorized"] and state["image_sharing_authorized_at"]
    actions = {entry["action"] for entry in client.get("/api/admin/audit-logs").json()["entries"]}
    assert "provider.image_sharing_authorized" in actions
    status = client.get("/api/rag/image-understanding").json()
    assert status["state"] == "supported" and status["available"] and status["model"] == "deepseek-flash"
    process_all(client)
    assert not described(first["id"]) and fake_provider.image_calls == []  # existing documents are not reprocessed

    # A model the catalog marks as text-only: reported unavailable, and no picture is sent anywhere.
    version = client.get(PROVIDER).json()["version"]
    assert client.put(PROVIDER, json={"model": "deepseek-v4-pro", "settings": {}, "expected_version": version}).status_code == 200
    assert client.get("/api/rag/image-understanding").json()["state"] == "unsupported"
    doc = upload(client, "plain-chart.pdf", make_pdf_with_chart("Plain introduction.", "Chart page text.")).json()
    process_all(client)
    assert not described(doc["id"]) and fake_provider.image_calls == []

    # A vision model whose provider refuses images, then one that times out: the text is indexed regardless.
    version = client.get(PROVIDER).json()["version"]
    assert client.put(PROVIDER, json={"model": "deepseek-flash", "settings": {}, "expected_version": version}).status_code == 200
    for reply in (None, LLMTimeoutError()):
        fake_provider.vision_reply = reply
        calls = len(fake_provider.image_calls)
        doc = upload(client, "refused.pdf", make_pdf_with_chart("Refused introduction.", "Refused chart page.")).json()
        process_all(client)
        assert len(fake_provider.image_calls) == calls + 1  # tried once
        assert not described(doc["id"]) and client.get(f"/api/rag/documents/{doc['id']}").json()["status"] == "ready"

    # A text-only PDF is unchanged: no pictures, no provider call.
    calls = len(fake_provider.image_calls)
    text_only = upload(client, "memo.pdf", make_pdf("A memo with text only.")).json()
    process_all(client)
    memo = chunks(text_only["id"])
    assert memo and "A memo with text only." in memo[0].content
    assert not described(text_only["id"]) and len(fake_provider.image_calls) == calls

    assert allow_images(client, authorized=False).status_code == 200  # withdrawn: nothing is sent again
    assert client.get("/api/rag/image-understanding").json()["state"] == "not_authorized"
    assert not client.get(PROVIDER).json()["image_sharing_authorized"]
