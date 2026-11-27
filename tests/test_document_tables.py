"""PDF and DOCX tables through the real pipeline (Docling): stored as rows and columns, indexed as whole-row chunks
that repeat the column names, and counted exactly by the existing table query engine. All data is synthetic; the
CRM export is a replica of the layout the WP1 defect was found on ("What is total leads?" answered 5, not 20)."""

import json
import os
import re
import sys

import docx
import pytest

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import (
    LEAD_COLUMNS,
    activate_provider,
    ask,
    make_pdf_with_table,
    process_all,
    synthetic_leads,
    upload,
)
from tests.test_access_control import set_access
from tests.test_document_formats import _save, chunks

QUESTION = "What is total leads?"


def stored(document_id: str) -> list:
    from sqlalchemy import text

    return run_db(
        lambda c: c.execute(
            text("SELECT table_index, title, locator_kind, columns, rows, row_positions, summary_rows, structured "
                 "FROM document_tables WHERE document_id = :d ORDER BY table_index"),
            {"d": document_id},
        )
    ).all()


def table_query(message: str, **query) -> str:
    """The fake LLM's reply to the table-query prompt: the query, on the first table listed."""
    table = re.search(r"^(T\d+) · ", message.split("TABLES in", 1)[1], re.M).group(1)
    return json.dumps({"table": table, **query})


def crm_upload(client) -> dict:
    activate_provider(client)  # signs in as the admin
    pdf = make_pdf_with_table("CRM Leads Export", LEAD_COLUMNS, synthetic_leads(20), trailing_pages=2)
    doc = upload(client, "CRM_Leads_Export_2026-09-11 (1).pdf", pdf).json()
    assert set_access(client, doc["id"], "restricted", users=[USER_EMAIL]).status_code == 200
    process_all(client)
    result = client.get(f"/api/rag/documents/{doc['id']}").json()
    assert result["status"] == "ready", result
    return doc


@pytest.mark.skipif(sys.platform == "win32", reason="Native Windows PDF Docling is disabled; run this table acceptance in Linux containers.")
def test_crm_leads_pdf_is_stored_chunked_and_counted_exactly(client, fake_provider):
    doc = crm_upload(client)

    table_rows = stored(doc["id"])
    [table] = table_rows
    assert table.structured and table.locator_kind == "page" and table.title == "CRM Leads Export"
    assert table.columns == LEAD_COLUMNS and len(table.rows) == 20 and table.row_positions == [1] * 20
    assert len({row[0] for row in table.rows}) == 20 and table.summary_rows == []

    rows = [row for row in chunks(doc["id"]) if (row.locator or {}).get("table") == 1]
    ids = [lead for row in rows for lead in re.findall(r"Row \d+: ID: (\d+);", row.content)]
    assert sorted(ids) == [str(n) for n in range(137, 157)]  # all 20 leads, none repeated by chunk overlap
    spans = [tuple(row.locator["rows"]) for row in rows]
    assert spans[0][0] == 1 and spans[-1][1] == 20 and all(b[0] == a[1] + 1 for a, b in zip(spans, spans[1:], strict=False))
    for row in rows:  # every chunk names the table and its columns, and holds whole records only
        assert row.content.startswith("CRM Leads Export · Table 1 · Rows ") and " of 20 (columns: ID, Name, Phone," in row.content
        assert row.page_number == 1 and row.locator["kind"] == "page" and row.locator["label"].startswith("Page 1 · Table 1, rows ")

    fake_provider.rag_reply = lambda m: table_query(m, operation="count") if "TABLES in" in m else "There are 20 leads [S1]."
    login(client, USER_EMAIL)
    body = ask(client, QUESTION).json()
    assert body["strategy"] == "table_analysis" and body["insufficient_evidence"] is False
    assert "20" in body["answer"] and "5 leads" not in body["answer"]
    [citation] = body["citations"]
    assert (citation["filename"], citation["page_number"]) == ("CRM_Leads_Export_2026-09-11 (1).pdf", 1)
    assert citation["location"] == "Page 1 · Table 1 (CRM Leads Export), computed over 20 rows"
    table_prompt = next(m for p, m in fake_provider.calls if p is not None and "TABLES in" in m)
    assert "T1 · Page 1 · Table 1 (CRM Leads Export) · 20 rows · columns: ID | Name | Phone" in table_prompt
    answer_prompt = fake_provider.calls[-1][1]
    assert "Computed exactly from all 20 data rows of Page 1 · Table 1 (CRM Leads Export)" in answer_prompt
    assert "| Row count |" in answer_prompt and "| 20 |" in answer_prompt

    login(client, USER2_EMAIL)  # no access: the table is neither queried nor cited
    calls = len(fake_provider.calls)
    body = ask(client, QUESTION).json()
    assert body["strategy"] != "table_analysis" and not body["citations"]
    assert not any("TABLES in" in m for _, m in fake_provider.calls[calls:])


@pytest.mark.skipif(not os.environ.get("INFRASPINE_REAL_CRM_PDF"), reason="set INFRASPINE_REAL_CRM_PDF to the stored CRM export")
def test_the_real_crm_export_has_exactly_20_leads_without_duplicates():
    """The development CRM export itself (customer data: read where it is stored, never copied into the repository
    and never sent to a provider): extraction, chunking and the table engine, with no LLM involved."""
    from app.core.config import get_settings
    from app.rag import tables
    from app.rag.isolation import parse_and_chunk
    from app.rag.parsers.registry import limits_from_settings

    settings = get_settings()
    _, chunk_list, _, found = parse_and_chunk(
        os.environ["INFRASPINE_REAL_CRM_PDF"], "pdf", limits_from_settings(settings), settings.rag_chunk_size,
        settings.rag_chunk_overlap, True,
    )
    [table] = found
    assert table.structured and len(table.rows) == 20 and len({row[0] for row in table.rows}) == 20
    assert table.columns[:2] == ("ID", "Name")  # the merged "ID Name" header cell is split
    ids = [lead for chunk in chunk_list for lead in re.findall(r"Row \d+: ID: (\d+);", chunk.text)]
    assert len(ids) == 20 and len(set(ids)) == 20  # every lead once across chunks
    engine_table = tables.Table("T1", "CRM", 1, 1, 0, table.columns, table.rows)
    _, _, matched, _, _ = tables.run_query(tables.parse_query('{"table": "T1", "operation": "count"}', [engine_table]))
    assert matched == 20


@pytest.mark.skipif(sys.platform == "win32", reason="Native Windows PDF Docling is disabled; run this table acceptance in Linux containers.")
def test_a_count_that_is_not_computed_is_never_read_off_the_retrieved_rows(client, fake_provider):
    doc = crm_upload(client)
    fake_provider.rag_reply = lambda m: "NONE" if "TABLES in" in m else "The export lists 20 leads [S1]."
    login(client, USER_EMAIL)
    body = ask(client, QUESTION).json()  # the query step declined: normal retrieval answers
    assert body["strategy"] != "table_analysis"
    answer_prompt = fake_provider.calls[-1][1]
    shown = len(set(re.findall(r"Row \d+: ID: (\d+);", answer_prompt)))
    assert shown < 20  # retrieval shows at most some of the rows ...
    assert (  # ... so the prompt also carries the table's real size
        f"Table facts for CRM_Leads_Export_2026-09-11 (1).pdf, Page 1 · Table 1 (CRM Leads Export), computed from the "
        f"whole stored table: it has exactly 20 data rows" in answer_prompt
    )
    assert f"show only {shown} of them" in answer_prompt and doc["id"]


@pytest.mark.skipif(sys.platform == "win32", reason="Native Windows PDF Docling is disabled; run this table acceptance in Linux containers.")
def test_coverage_notes_name_the_real_size_of_partly_retrieved_and_unread_tables(client):
    """Unit-level: rows 1–6 of a 20-row table, a document whose table rows were not retrieved, a full table."""
    import asyncio
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.rag import tables
    from app.rag.retrieval import RetrievedChunk
    from app.db.session import test_engine_options
    from tests.conftest import TEST_DATABASE_URL

    doc = crm_upload(client)
    document_id = uuid.UUID(doc["id"])

    def passage(locator: dict) -> RetrievedChunk:
        return RetrievedChunk(1, document_id, "crm.pdf", 1, 0, "text", 0.5, locator)

    async def notes(passages):
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
        try:
            async with AsyncSession(engine) as db:
                return await tables.coverage_notes(db, passages)
        finally:
            await engine.dispose()

    partial = asyncio.run(notes([passage({"kind": "page", "table": 1, "rows": [1, 3]}), passage({"kind": "page", "table": 1, "rows": [4, 6]})]))
    assert len(partial) == 1 and "exactly 20 data rows" in partial[0].content and "show only 6 of them" in partial[0].content
    assert partial[0].locator["computed"] and partial[0].page_number == 1
    unread = asyncio.run(notes([passage({"kind": "page", "position": 1})]))
    assert len(unread) == 1 and "show only 0 of them" in unread[0].content
    full = asyncio.run(notes([passage({"kind": "page", "table": 1, "rows": [1, 20]})]))
    assert full == []


@pytest.mark.skipif(sys.platform == "win32", reason="Native Windows PDF Docling is disabled; run this table acceptance in Linux containers.")
def test_a_table_continued_over_pages_is_one_table_with_rows_cited_to_their_page(client, fake_provider):
    activate_provider(client)
    pdf = make_pdf_with_table("CRM Leads Export", LEAD_COLUMNS, synthetic_leads(30), rows_per_page=18, repeat_header=False)
    doc = upload(client, "leads-two-pages.pdf", pdf).json()
    process_all(client)

    table_rows = stored(doc["id"])
    assert len(table_rows) == 1, [
        (row.title[:32], len(row.columns), len(row.rows), row.rows[0][:3] if row.rows else (), row.structured)
        for row in table_rows
    ]
    [table] = table_rows
    assert table.structured and len(table.rows) == 30 and table.row_positions == [1] * 18 + [2] * 12
    pages = {tuple(row.locator["rows"]): row.page_number for row in chunks(doc["id"]) if (row.locator or {}).get("table")}
    assert all(pages[span] == (1 if span[1] <= 18 else 2) for span in pages) and set(pages.values()) == {1, 2}

    fake_provider.rag_reply = lambda m: (
        table_query(m, operation="count", filters=[{"column": "Status", "op": "eq", "value": "closed"}])
        if "TABLES in" in m else "10 leads are closed [S1]."
    )
    body = ask(client, "How many leads are closed?", [doc["id"]]).json()
    assert body["strategy"] == "table_analysis"
    assert body["citations"][0]["location"] == "Pages 1–2 · Table 1 (CRM Leads Export), computed over 30 rows"
    closed = sum(1 for lead in synthetic_leads(30) if lead[6] == "closed")
    assert f"Rows matching: {closed}." in fake_provider.calls[-1][1]


def test_docx_tables_are_stored_without_their_total_row(client, fake_provider):
    document_ = docx.Document()
    document_.add_heading("Payments for approval", level=1)
    document_.add_paragraph("The IT department requests approval for these internet links.")
    table = document_.add_table(rows=4, cols=3)
    values = (("Sr", "Description", "Amount (Rs.)"), ("1", "Internet link A 100mbps", "32,982"),
              ("2", "Internet link B 50mbps", "5,317"), ("Total", "", "38,299"))
    for row, cells in zip(table.rows, values, strict=True):
        for cell, value in zip(row.cells, cells, strict=True):
            cell.text = value
    activate_provider(client)
    doc = upload(client, "internet-bills.docx", _save(document_), content_type="application/octet-stream").json()
    process_all(client)

    [stored_table] = stored(doc["id"])
    assert stored_table.locator_kind == "section" and stored_table.structured
    assert stored_table.columns == ["Sr", "Description", "Amount (Rs.)"] and len(stored_table.rows) == 2
    assert stored_table.summary_rows == [["Total", "", "38,299"]]
    [chunk] = [row for row in chunks(doc["id"]) if (row.locator or {}).get("table") == 1]
    assert chunk.locator["kind"] == "section" and chunk.locator["label"] == "Payments for approval · Table 1, rows 1–2"
    assert "Row 2: Sr: 2; Description: Internet link B 50mbps; Amount (Rs.): 5,317" in chunk.content
    assert "Total row: Sr: Total; Amount (Rs.): 38,299" in chunk.content

    fake_provider.rag_reply = lambda m: (
        table_query(m, operation="sum", value="Amount (Rs.)") if "TABLES in" in m else "The total is 38,299 [S1]."
    )
    body = ask(client, "What is the total amount?", [doc["id"]]).json()
    assert body["strategy"] == "table_analysis"
    assert "| 38299 |" in fake_provider.calls[-1][1]  # the two links, not doubled by the total row
    login(client, ADMIN_EMAIL)
