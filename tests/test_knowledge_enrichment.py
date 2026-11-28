"""Step AB slices 2–3 — ingestion hook, backfill and daily expiry-alert check.

DB-backed tests on the throwaway *_test database (see tests/conftest.py).
Run with pytest and log capture on.
"""

from datetime import date
from functools import partial
import uuid

import pytest
from sqlalchemy import select

from app.db.models import DOC_READY, Document, DocumentChunk, KnowledgeExpiryAlert, User
from app.main import app
from app.rag.enrichment import apply_enrichment, backfill_enrichment, enrichment_metadata
from app.rag.expiry_alerts import (
    mark_alerts_delivered,
    pending_alerts_for_digest,
    run_expiry_alert_check,
)
from tests.conftest import USER_EMAIL, login, run_db
from tests.rag_helpers import make_pdf, ready

CONTRACT_TEXT = (
    "This agreement is entered into between Acme Ltd and Beta Corp. "
    "The parties hereby agree to the terms and conditions set out below. "
    "The governing law is the law of Pakistan. "
    "This contract is valid until 31 December 2026."
)

PLAIN_TEXT = "Weekly notes about the office plants and the new coffee machine."

REFERENCE_DATE = date(2026, 11, 28)


def _seed_ready_document(filename, chunk_texts, *, expiry=None, status=DOC_READY, enriched_at=None):
    """Insert a ready Document + chunks directly; returns the document id (str)."""

    async def _seed(conn):
        user_id = (await conn.execute(select(User.id).where(User.email == USER_EMAIL))).scalar_one()
        doc_id = uuid.uuid4()
        await conn.execute(
            Document.__table__.insert().values(
                id=doc_id,
                owner_id=user_id,
                visibility="private",
                source_system="upload",
                filename=filename,
                file_format="txt",
                storage_key=f"test-{doc_id}",
                sha256="0" * 64,
                size_bytes=100,
                status=status,
                expiry_date=expiry,
                enriched_at=enriched_at,
            )
        )
        for index, text in enumerate(chunk_texts):
            await conn.execute(
                DocumentChunk.__table__.insert().values(
                    document_id=doc_id,
                    chunk_index=index,
                    page_number=1,
                    locator=None,
                    content=text,
                    embedding=b"",
                )
            )
        return str(doc_id)

    return run_db(_seed)


def _read_document(doc_id):
    async def _read(conn):
        # NOTE: a Core connection returns raw column rows, not ORM entities,
        # so select the columns explicitly (an ORM Session would be needed
        # for `select(Document)` to return a Document).
        row = (
            await conn.execute(
                select(
                    Document.tags,
                    Document.summary,
                    Document.expiry_date,
                    Document.expiry_dates,
                    Document.enriched_at,
                    Document.status,
                ).where(Document.id == uuid.UUID(doc_id))
            )
        ).one()
        return {
            "tags": row.tags,
            "summary": row.summary,
            "expiry_date": row.expiry_date,
            "expiry_dates": row.expiry_dates,
            "enriched_at": row.enriched_at,
            "status": row.status,
        }

    return run_db(_read)


def _alert_rows():
    async def _read(conn):
        rows = (
            await conn.execute(
                select(
                    KnowledgeExpiryAlert.id,
                    KnowledgeExpiryAlert.document_id,
                    KnowledgeExpiryAlert.expiry_date,
                    KnowledgeExpiryAlert.urgency,
                    KnowledgeExpiryAlert.days_left,
                    KnowledgeExpiryAlert.status,
                ).order_by(KnowledgeExpiryAlert.expiry_date)
            )
        ).all()
        return [
            {
                "id": row.id,
                "document_id": str(row.document_id),
                "expiry_date": row.expiry_date,
                "urgency": row.urgency,
                "days_left": row.days_left,
                "status": row.status,
            }
            for row in rows
        ]

    return run_db(_read)


# ---------------------------------------------------------------------------
# Pure enrichment (no DB)
# ---------------------------------------------------------------------------


def test_enrichment_metadata_contract():
    meta = enrichment_metadata(CONTRACT_TEXT)
    assert "contract" in meta["tags"]
    assert meta["summary"], "a contract should get an extractive summary"
    # Extractive only: every summary sentence comes verbatim from the text.
    for sentence in meta["summary"].split(". "):
        assert sentence.strip().rstrip(".") in CONTRACT_TEXT
    assert meta["expiry_date"] == "2026-12-31"
    assert meta["expiry_dates"][0]["label"] in ("valid until", "validity", "expiry")


def test_enrichment_metadata_gibberish_has_nothing():
    meta = enrichment_metadata("zxqv blorp wumpus fnord")
    assert meta["tags"] == []
    # Short text is returned as-is by the extractive summarizer (slice 1 rule);
    # the point is nothing is invented: no tags, no dates.
    assert meta["summary"] == "zxqv blorp wumpus fnord"
    assert meta["expiry_date"] is None
    assert meta["expiry_dates"] == []


def test_apply_enrichment_writes_metadata_only():
    doc = Document()
    meta = apply_enrichment(doc, CONTRACT_TEXT)
    assert doc.tags == meta["tags"]
    assert doc.summary
    assert doc.expiry_date == date(2026, 12, 31)
    assert doc.expiry_dates[0]["date"] == "2026-12-31"
    assert doc.enriched_at is not None


# ---------------------------------------------------------------------------
# Slice 2 — ingestion hook + backfill
# ---------------------------------------------------------------------------


def test_ingestion_hook_enriches_on_ready(client):
    login(client, USER_EMAIL)
    result = ready(client, "contract.pdf", make_pdf(CONTRACT_TEXT))
    assert result["status"] == "ready"
    doc = _read_document(result["id"])
    assert "contract" in doc["tags"]
    assert doc["summary"], "ingested contract should have a summary"
    assert doc["expiry_date"] == date(2026, 12, 31)
    assert doc["expiry_dates"][0]["date"] == "2026-12-31"
    assert doc["enriched_at"] is not None


def test_ingestion_hook_plain_document_has_no_expiry(client):
    login(client, USER_EMAIL)
    result = ready(client, "notes.pdf", make_pdf(PLAIN_TEXT))
    assert result["status"] == "ready"
    doc = _read_document(result["id"])
    assert doc["expiry_date"] is None
    assert doc["expiry_dates"] is None
    assert doc["enriched_at"] is not None


def test_backfill_enriches_ready_documents_from_chunks(client):
    contract_id = _seed_ready_document("contract.txt", [CONTRACT_TEXT])
    plain_id = _seed_ready_document("notes.txt", [PLAIN_TEXT])
    result = client.portal.call(partial(backfill_enrichment, app.state.sessionmaker, limit=10))
    assert result == {"examined": 2, "enriched": 2, "skipped": 0, "errors": 0}

    contract = _read_document(contract_id)
    assert "contract" in contract["tags"]
    assert contract["expiry_date"] == date(2026, 12, 31)
    assert contract["enriched_at"] is not None

    plain = _read_document(plain_id)
    assert plain["tags"] == []
    assert plain["expiry_date"] is None
    assert plain["enriched_at"] is not None


def test_backfill_is_idempotent_and_skips_enriched(client):
    _seed_ready_document("contract.txt", [CONTRACT_TEXT])
    first = client.portal.call(partial(backfill_enrichment, app.state.sessionmaker, limit=10))
    assert first["examined"] == 1 and first["enriched"] == 1
    second = client.portal.call(partial(backfill_enrichment, app.state.sessionmaker, limit=10))
    assert second == {"examined": 0, "enriched": 0, "skipped": 0, "errors": 0}


def test_backfill_respects_limit(client):
    for index in range(3):
        _seed_ready_document(f"doc-{index}.txt", [PLAIN_TEXT])
    result = client.portal.call(partial(backfill_enrichment, app.state.sessionmaker, limit=2))
    assert result["examined"] == 2 and result["enriched"] == 2
    rest = client.portal.call(partial(backfill_enrichment, app.state.sessionmaker, limit=10))
    assert rest["examined"] == 1 and rest["enriched"] == 1


# ---------------------------------------------------------------------------
# Slice 3 — daily expiry-alert check
# ---------------------------------------------------------------------------


def _seed_alert_documents():
    expired_id = _seed_ready_document("expired.txt", [CONTRACT_TEXT], expiry=date(2026, 11, 1))
    due_id = _seed_ready_document("due.txt", [CONTRACT_TEXT], expiry=date(2026, 12, 8))
    ok_id = _seed_ready_document("ok.txt", [CONTRACT_TEXT], expiry=date(2027, 2, 1))
    none_id = _seed_ready_document("none.txt", [CONTRACT_TEXT])
    pending_id = _seed_ready_document("pending.txt", [CONTRACT_TEXT], expiry=date(2026, 11, 1), status="pending")
    return expired_id, due_id, ok_id, none_id, pending_id


def test_expiry_alert_check_creates_rows_for_expired_and_due_soon(client):
    expired_id, due_id, _ok_id, _none_id, _pending_id = _seed_alert_documents()
    result = client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=30)
    )
    assert result == {"checked": 3, "expired": 1, "due_soon": 1, "inserted": 2}

    rows = _alert_rows()
    assert len(rows) == 2
    by_doc = {row["document_id"]: row for row in rows}
    assert by_doc[expired_id]["urgency"] == "expired"
    assert by_doc[expired_id]["days_left"] == -27
    assert by_doc[due_id]["urgency"] == "due_soon"
    assert by_doc[due_id]["days_left"] == 10
    assert all(row["status"] == "pending" for row in rows)


def test_expiry_alert_check_is_idempotent_same_day(client):
    _seed_alert_documents()
    first = client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=30)
    )
    assert first["inserted"] == 2
    second = client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=30)
    )
    assert second["inserted"] == 0
    assert len(_alert_rows()) == 2


def test_expiry_alert_check_next_day_creates_new_rows(client):
    _seed_alert_documents()
    client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=30)
    )
    result = client.portal.call(
        partial(
            run_expiry_alert_check, app.state.sessionmaker, reference_date=date(2026, 11, 29), warn_days=30
        )
    )
    assert result["inserted"] == 2
    assert len(_alert_rows()) == 4


def test_pending_alerts_for_digest_and_mark_delivered(client):
    _seed_alert_documents()
    client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=30)
    )
    digest = client.portal.call(
        partial(pending_alerts_for_digest, app.state.sessionmaker, checked_on=REFERENCE_DATE)
    )
    assert len(digest) == 2
    assert digest[0]["urgency"] == "expired"  # earliest expiry first
    assert digest[0]["filename"] == "expired.txt"
    assert digest[0]["expiry_date"] == "2026-11-01"
    assert digest[0]["days_left"] == -27

    delivered = client.portal.call(
        partial(mark_alerts_delivered, app.state.sessionmaker, [uuid.UUID(row["alert_id"]) for row in digest])
    )
    assert delivered == 2
    remaining = client.portal.call(
        partial(pending_alerts_for_digest, app.state.sessionmaker, checked_on=REFERENCE_DATE)
    )
    assert remaining == []


def test_expiry_alert_check_warn_window_is_respected(client):
    _seed_ready_document("due.txt", [CONTRACT_TEXT], expiry=date(2026, 12, 8))
    result = client.portal.call(
        partial(run_expiry_alert_check, app.state.sessionmaker, reference_date=REFERENCE_DATE, warn_days=7)
    )
    # 10 days out is beyond a 7-day window: checked, but no alert row.
    assert result == {"checked": 1, "expired": 0, "due_soon": 0, "inserted": 0}
    assert _alert_rows() == []
