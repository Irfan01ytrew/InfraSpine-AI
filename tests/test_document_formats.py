"""Multi-format ingestion: content-based detection, per-format sections and locators, citations, explicit
failures and permission isolation. End to end through the API, worker, PostgreSQL and FAISS (fake embedder
and fake LLM). All document content is invented."""

import io
import json
import zipfile

import docx
import openpyxl
import pytest
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import text

from app.core.config import get_settings
from tests.conftest import ADMIN_EMAIL, RAG_TEST_DIR, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, ask, document, make_pdf, process_all, replace_file, upload

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
WORD_MAIN = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


# ------------------------------------------------------------------ synthetic files


def make_docx() -> bytes:
    document_ = docx.Document()
    document_.add_paragraph("This handbook describes internal IT practices.")
    document_.add_heading("Remote access", level=1)
    document_.add_paragraph("VPN tokens are rotated every 90 days by the security operations team.")
    document_.add_heading("Laptops", level=2)
    table = document_.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, (("Model", "Refresh cycle"), ("Contoso Book 14", "four years")), strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    document_.add_heading("Printing", level=1)
    document_.add_paragraph("Badge release printing is enabled on every floor.")
    return _save(document_)


def make_xlsx(rows: int = 2) -> bytes:
    workbook = openpyxl.Workbook()
    inventory = workbook.active
    inventory.title = "Inventory"
    inventory.append(["Asset", "Location", "Units"])
    data = [("Cooling unit", "Hall A", 12), ("UPS battery", "Hall B", 40)]
    data += [(f"Rack sensor {i}", f"Row {i}", i) for i in range(rows - 2)]
    for row in data[:rows]:
        inventory.append(list(row))
    budget = workbook.create_sheet("Budget")
    budget.append(["Item", "Amount"])
    budget.append(["Chiller replacement", 125000.0])
    return _save(workbook)


def make_pptx() -> bytes:
    presentation = Presentation()
    for title, body, notes in (
        ("Quarterly review", "Ticket volume fell by a third.", ""),
        ("Datacenter cooling", "Hall A chillers run at 60 percent capacity.", "Mention the summer peak plan."),
    ):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    table_slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    table_slide.shapes.title.text = "Support hours"
    table = table_slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for (r, c), value in {(0, 0): "Region", (0, 1): "Hours", (1, 0): "EMEA", (1, 1): "07:00 to 19:00"}.items():
        table.cell(r, c).text = value
    return _save(presentation)


def make_csv(delimiter: str = ",") -> bytes:
    lines = ["Host,Owner,Patch window", "db01,platform team,Sunday 02:00", "web01,web team,Saturday 23:00"]
    return "\n".join(line.replace(",", delimiter) for line in lines).encode()


def _save(document_) -> bytes:
    buffer = io.BytesIO()
    document_.save(buffer)
    return buffer.getvalue()


def rezip(data: bytes, replace: dict[str, bytes] | None = None, add: dict[str, bytes] | None = None,
          encrypted_flag: str | None = None) -> bytes:
    """Rewrite an Office package: replace or add parts, or mark one part as encrypted (flag bit 0)."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = (replace or {}).get(info.filename, source.read(info.filename))
            target.writestr(zipfile.ZipInfo(info.filename, info.date_time), payload, zipfile.ZIP_DEFLATED)
        for name, payload in (add or {}).items():
            target.writestr(name, payload)
    data = bytearray(out.getvalue())
    if encrypted_flag:  # zipfile resets flags on write, so set the bit in both headers afterwards
        name = encrypted_flag.encode()
        for signature, flag_offset, name_offset in ((b"PK", 6, 30), (b"PK", 8, 46)):
            start = data.find(signature)
            while start != -1:
                if data[start + name_offset : start + name_offset + len(name)] == name:
                    data[start + flag_offset] |= 0x1
                start = data.find(signature, start + 4)
    return bytes(data)


# ------------------------------------------------------------------ helpers


def chunks(document_id: str) -> list:
    return run_db(
        lambda c: c.execute(
            text("SELECT page_number, locator, content FROM document_chunks WHERE document_id = :d ORDER BY chunk_index"),
            {"d": document_id},
        )
    ).all()


def uploaded_files() -> set[str]:
    folder = RAG_TEST_DIR / "uploads"
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def ready(client, filename: str, data: bytes, **kwargs) -> dict:
    response = upload(client, filename, data, content_type="application/octet-stream", **kwargs)
    assert response.status_code == 202, response.text
    process_all(client)
    doc = document(client, response.json()["id"])
    assert doc["status"] == "ready", doc
    return doc


def cite_first(message: str) -> str:
    return "Here is the answer [S1]."


# ------------------------------------------------------------------ formats and locators


def test_docx_sections_follow_headings_and_are_cited_by_heading(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = ready(client, "handbook.docx", make_docx())
    assert (doc["file_format"], doc["page_count"]) == ("docx", None)

    rows = chunks(doc["id"])
    by_label = {r.locator["label"]: r for r in rows}
    assert list(by_label) == ["Document start", "Remote access", "Remote access > Laptops · Table 1, row 1", "Printing"]
    laptops = by_label["Remote access > Laptops · Table 1, row 1"]  # a table: whole rows with their column names
    assert laptops.locator["kind"] == "section" and laptops.locator["heading_path"] == ["Remote access", "Laptops"]
    assert (laptops.locator["table"], laptops.locator["rows"]) == (1, [1, 1])
    assert "Row 1: Model: Contoso Book 14; Refresh cycle: four years" in laptops.content
    assert [r.page_number for r in rows] == [1, 2, 3, 4]  # positions = section ordinals

    fake_provider.rag_reply = cite_first
    body = ask(client, "How often are VPN tokens rotated by security operations?").json()
    [citation] = body["citations"]
    assert citation["location"] == "Remote access" and citation["locator"]["kind"] == "section"
    assert "90 days" in citation["snippet"]
    [(_, message)] = fake_provider.calls
    assert 'location="Remote access"' in message


def test_xlsx_keeps_sheet_names_columns_and_cell_references(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = ready(client, "assets.xlsx", make_xlsx())
    assert (doc["file_format"], doc["page_count"]) == ("xlsx", 2)

    inventory, budget = chunks(doc["id"])
    assert inventory.locator == {
        "kind": "sheet", "position": 1, "label": "Sheet 'Inventory' A2:C3", "sheet": "Inventory", "sheet_index": 1,
        "range": "A2:C3", "start_row": 2, "end_row": 3,
    }
    assert inventory.content.startswith("Sheet 'Inventory' (columns: A Asset, B Location, C Units), rows 2–3")
    assert "Row 3: Asset (A3): UPS battery; Location (B3): Hall B; Units (C3): 40" in inventory.content
    assert budget.locator["sheet"] == "Budget" and budget.page_number == 2
    assert "Amount (B2): 125000" in budget.content

    fake_provider.rag_reply = lambda message: "Hall B has 40 UPS battery units [S1]."
    body = ask(client, "How many UPS battery units are in Hall B?").json()
    assert body["citations"][0]["location"] == "Sheet 'Inventory' A2:C3"
    assert body["citations"][0]["locator"]["range"] == "A2:C3"
    assert "location=\"Sheet 'Inventory' A2:C3\"" in fake_provider.calls[-1][1]


def test_large_sheets_split_into_self_describing_row_blocks(client):
    login(client, USER_EMAIL)
    doc = ready(client, "sensors.xlsx", make_xlsx(rows=300))
    inventory = [r for r in chunks(doc["id"]) if r.locator["sheet"] == "Inventory"]
    assert len(inventory) > 3
    expected_start = 2
    for row in inventory:  # contiguous, gap-free row ranges; every block repeats the sheet and its columns
        assert row.locator["start_row"] == expected_start
        assert row.content.startswith("Sheet 'Inventory' (columns: A Asset, B Location, C Units)")
        assert len(row.content) <= get_settings().rag_chunk_size
        expected_start = row.locator["end_row"] + 1
    assert expected_start == 302


def test_pptx_slides_titles_tables_and_notes(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = ready(client, "review.pptx", make_pptx())
    assert (doc["file_format"], doc["page_count"]) == ("pptx", 3)

    rows = chunks(doc["id"])
    assert [r.locator["label"] for r in rows] == [
        "Slide 1 – Quarterly review", "Slide 2 – Datacenter cooling", "Slide 3 – Support hours"
    ]
    assert "Speaker notes: Mention the summer peak plan." in rows[1].content
    assert "EMEA | 07:00 to 19:00" in rows[2].content
    assert [r.page_number for r in rows] == [1, 2, 3]

    fake_provider.rag_reply = cite_first
    citation = ask(client, "What capacity do the Hall A chillers run at?").json()["citations"][0]
    assert (citation["page_number"], citation["locator"]["slide"]) == (2, 2)
    assert citation["location"] == "Slide 2 – Datacenter cooling"


def test_txt_line_ranges_and_legacy_encodings(client):
    login(client, USER_EMAIL)
    lines = [f"Runbook step {i}: verify the backup job for cluster {i} completed." for i in range(1, 61)]
    doc = ready(client, "runbook.txt", ("\n".join(lines) + "\nCafé network closes at noon.").encode("cp1252"))
    assert (doc["file_format"], doc["page_count"]) == ("txt", None)
    rows = chunks(doc["id"])
    assert rows[0].locator["start_line"] == 1 and rows[-1].locator["end_line"] == 61
    for previous, current in zip(rows, rows[1:]):
        assert current.locator["start_line"] == previous.locator["end_line"] + 1
    assert rows[0].locator["label"].startswith("Lines 1–")
    assert "Café network closes at noon." in rows[-1].content


@pytest.mark.parametrize("delimiter", [",", ";"])
def test_csv_rows_keep_their_headers(client, delimiter):
    login(client, USER_EMAIL)
    doc = ready(client, "patching.csv", make_csv(delimiter))
    [row] = chunks(doc["id"])
    assert row.locator == {"kind": "rows", "position": 1, "label": "Rows 2–3", "start_row": 2, "end_row": 3}
    assert row.content.startswith("Rows 2–3 (columns: Host, Owner, Patch window)")
    assert "Row 2: Host: db01; Owner: platform team; Patch window: Sunday 02:00" in row.content


def test_pdf_citations_keep_pages_including_chunks_indexed_before_locators(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = ready(client, "handbook.pdf", make_pdf("Cover page.", "Refund requests are accepted within 30 days."))
    assert [r.locator for r in chunks(doc["id"])] == [
        {"kind": "page", "position": 1, "label": "Page 1", "page": 1},
        {"kind": "page", "position": 2, "label": "Page 2", "page": 2},
    ]
    fake_provider.rag_reply = lambda message: "Refund requests are accepted within 30 days [S1]."
    question = "Within how many days are refund requests accepted?"
    expected = {"page_number": 2, "location": "Page 2", "locator": {"kind": "page", "position": 2, "label": "Page 2", "page": 2}}
    citation = ask(client, question).json()["citations"][0]
    assert {k: citation[k] for k in expected} == expected
    assert 'page="2"' in fake_provider.calls[-1][1]

    run_db(lambda c: c.execute(text("UPDATE document_chunks SET locator = NULL")))  # as indexed by older versions
    legacy = ask(client, question).json()["citations"][0]
    assert {k: legacy[k] for k in expected} == expected


def test_replacement_can_change_format_and_keeps_the_old_version_on_rejection(client):
    login(client, USER_EMAIL)
    doc = ready(client, "policy.pdf", make_pdf("Old policy text about badge printing."))
    mismatch = replace_file(client, doc["id"], "policy.docx", make_pdf("pdf bytes behind a docx name"))
    assert mismatch.status_code == 415
    assert document(client, doc["id"])["file_format"] == "pdf"

    replaced = client.put(
        f"/api/rag/documents/{doc['id']}/file", files={"file": ("policy.docx", make_docx(), "application/octet-stream")}
    )
    assert replaced.status_code == 202 and replaced.json()["file_format"] == "docx"
    process_all(client)
    assert {r.locator["kind"] for r in chunks(doc["id"])} == {"section"}
    assert document(client, doc["id"])["page_count"] is None


# ------------------------------------------------------------------ detection and explicit failures


def _docx_with_dtd() -> bytes:
    original = make_docx()
    part = zipfile.ZipFile(io.BytesIO(original)).read("word/document.xml")
    return rezip(original, replace={"word/document.xml": part.replace(b"?>", b'?><!DOCTYPE d [<!ENTITY x "y">]>', 1)})


def _macro_docx() -> bytes:
    original = make_docx()
    types = zipfile.ZipFile(io.BytesIO(original)).read("[Content_Types].xml")
    return rezip(original, replace={"[Content_Types].xml": types.replace(WORD_MAIN, b"application/vnd.ms-word.document.macroEnabled.main+xml")})


@pytest.mark.parametrize(
    ("filename", "data", "reason"),
    [
        ("report.pdf", make_docx(), "not_a_pdf"),  # Word content behind a .pdf name
        ("report.docx", make_pdf("x"), "file_type_mismatch"),
        ("sheet.xlsx", make_docx(), "file_type_mismatch"),
        ("notes.txt", make_xlsx(), "file_type_mismatch"),  # a ZIP package is never treated as text
        ("data.csv", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" * 20, "unsupported_file_type"),
        ("archive.docx", rezip(make_docx(), replace={"[Content_Types].xml": b"<Types/>"}), "unsupported_file_type"),
        ("macros.docx", _macro_docx(), "unsupported_file_type"),
        # These are damaged CFB headers, not well-formed legacy/encrypted Office containers.
        ("legacy.xlsx", CFB_MAGIC + b"\x00" * 600, "invalid_file"),
        ("locked.docx", CFB_MAGIC + b"\x00" * 200 + "EncryptionInfo".encode("utf-16-le") + b"\x00" * 200, "invalid_file"),
        ("locked2.docx", rezip(make_docx(), encrypted_flag="word/document.xml"), "encrypted_file"),
        ("entities.docx", _docx_with_dtd(), "unsafe_file"),
        ("broken.xlsx", b"PK\x03\x04" + b"\x00" * 300, "invalid_file"),
        ("broken.pptx", rezip(make_pptx(), replace={"ppt/presentation.xml": b"<not-xml"}), "invalid_file"),
        ("empty.txt", b"", "empty_file"),
    ],
    ids=lambda value: value if isinstance(value, str) else "data",
)
def test_unsupported_unsafe_and_malformed_files_are_rejected_explicitly(client, filename, data, reason):
    login(client, USER_EMAIL)
    response = upload(client, filename, data, content_type="application/octet-stream")
    assert response.status_code == 415, response.text
    assert response.json()["error"]["details"]["reason"] == reason
    assert uploaded_files() == set()  # nothing rejected is kept
    assert client.get("/api/rag/documents").json()["documents"] == []


def test_oversized_packages_and_text_are_rejected(client, monkeypatch):
    login(client, USER_EMAIL)
    monkeypatch.setattr(get_settings(), "rag_max_unpacked_mb", 1)
    bomb = rezip(make_docx(), add={"word/media/filler.bin": b"\x00" * (3 * 1024 * 1024)})
    assert len(bomb) < 100_000  # tiny on the wire, 3 MB unpacked
    response = upload(client, "bomb.docx", bomb, content_type="application/octet-stream")
    assert (response.status_code, response.json()["error"]["details"]["reason"]) == (415, "file_too_large")

    monkeypatch.setattr(get_settings(), "rag_max_extracted_chars", 10_000)
    long_text = ("Inventory reconciliation note. " * 400).encode()
    response = upload(client, "long.txt", long_text, content_type="text/plain")
    assert (response.status_code, response.json()["error"]["details"]["reason"]) == (415, "text_too_large")
    assert uploaded_files() == set()


def test_files_without_text_fail_with_an_explicit_state(client):
    login(client, USER_EMAIL)
    empty = openpyxl.Workbook()
    response = upload(client, "blank.xlsx", _save(empty), content_type="application/octet-stream")
    assert response.status_code == 202  # structurally valid; content is only known after extraction
    process_all(client)
    doc = document(client, response.json()["id"])
    assert (doc["status"], doc["error_code"], doc["searchable"]) == ("failed", "no_text", False)
    assert chunks(doc["id"]) == []


# ------------------------------------------------------------------ permissions


def test_new_formats_follow_document_permissions(client, fake_provider):
    activate_provider(client)
    secret = "4.2 million euros"  # only in the documents, never in a question
    login(client, USER_EMAIL)
    workbook = openpyxl.Workbook()
    workbook.active.append(["Topic", "Detail"])
    workbook.active.append(["Merger codename Heron budget ceiling", secret])
    private = ready(client, "strategy.xlsx", _save(workbook))
    notes = ready(client, "notes.txt", f"The Merger codename Heron budget ceiling is {secret}.".encode())

    fake_provider.rag_reply = cite_first
    for email in (USER2_EMAIL, ADMIN_EMAIL):  # neither another user nor an admin can read a private upload
        login(client, email)
        fake_provider.calls.clear()
        assert client.get(f"/api/rag/documents/{private['id']}").status_code == 404
        body = ask(client, "What is the Merger codename Heron budget ceiling?").json()
        assert body["citations"] == [] and body["retrieved_chunks"] == 0
        assert all(secret not in message for _, message in fake_provider.calls)
        targeted = ask(client, "Heron budget ceiling", document_ids=[private["id"], notes["id"]]).json()
        assert targeted["retrieved_chunks"] == 0

    login(client, USER_EMAIL)  # the owner can, with sheet locators
    body = ask(client, "What is the Merger codename Heron budget ceiling?").json()
    assert body["citations"] and body["citations"][0]["document_id"] in {private["id"], notes["id"]}

    # Without LibreOffice the owner still gets the built-in Python preview of an Office page, but a private
    # file is never rendered for anyone else.
    owner_page = client.get(f"/api/rag/documents/{private['id']}/pages/1/image")
    assert owner_page.status_code == 200 and owner_page.headers["content-type"] == "image/png"
    for email in (USER2_EMAIL, ADMIN_EMAIL):
        login(client, email)
        assert client.get(f"/api/rag/documents/{private['id']}/pages/1/image").status_code == 404
    login(client, USER_EMAIL)
    images = ask(client, "Show me images of this report", document_ids=[private["id"]]).json()
    assert images["page_images"] == [] and "needs LibreOffice" in images["answer"]
    audit = run_db(lambda c: c.execute(text("SELECT details::text FROM audit_logs WHERE action = 'document.uploaded'"))).all()
    assert {json.loads(r[0])["format"] for r in audit} == {"xlsx", "txt"}


def test_word_pages_are_shown_through_a_cached_pdf_rendering(client, fake_provider, monkeypatch, tmp_path):
    import sys
    from pathlib import Path

    from app.main import app
    from app.rag import office_preview

    conversions = []

    def fake_convert(converter, source, work, timeout_seconds):  # stands in for headless LibreOffice
        conversions.append(source.suffix)
        out = work / f"{source.stem}.pdf"
        out.write_bytes(make_pdf("Rendered page one.", "Rendered page two."))
        return out

    monkeypatch.setattr(get_settings(), "rag_office_converter", sys.executable)  # any existing file
    monkeypatch.setattr(office_preview, "_convert", fake_convert)
    activate_provider(client)
    doc = upload(client, "handbook.docx", make_docx(), visibility="shared").json()
    process_all(client)

    body = ask(client, "show img", document_ids=[doc["id"]]).json()
    assert body["strategy"] == "page_images" and [i["page_number"] for i in body["page_images"]] == [1, 2]
    image = client.get(body["page_images"][1]["url"])
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/3/image").status_code == 404
    assert conversions == [".docx"]  # converted once, then served from the cache

    stored = run_db(lambda c: c.scalar(text("SELECT storage_key FROM documents")))
    preview = Path(app.state.rag.storage.preview_path(stored))
    assert preview.is_file()
    assert client.delete(f"/api/rag/documents/{doc['id']}").status_code == 204
    assert not preview.exists()  # the rendering goes with the document

    txt = upload(client, "notes.txt", b"Plain notes about printing.", visibility="shared").json()
    process_all(client)
    plain = ask(client, "show img", document_ids=[txt["id"]]).json()
    assert plain["page_images"] == [] and "plain text" in plain["answer"]
