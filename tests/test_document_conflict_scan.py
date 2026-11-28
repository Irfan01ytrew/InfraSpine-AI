"""Step AI slice 3 tests: nightly contradiction scan over newly indexed passages.

Runs against a dedicated *_test PostgreSQL database (see tests/conftest.py).
The scan is advisory: it stores chunk-id pairs only, never passage text, and
never changes any document, chunk, or candidate status.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Document, DocumentChunk, User
from app.db.session import test_engine_options as _test_engine_options
from app.rag.freshness_scan import ordered_pair, scan_new_chunks
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, run_db

NOW = datetime.now(timezone.utc)
KEEP = "Employees must keep all leave records for five years."
DELETE = "Employees must delete all leave records after one year."
UNRELATED_A = "Annual leave is twenty days per year."
UNRELATED_B = "Laptops must be returned when employment ends."


def _user_id(email: str) -> uuid.UUID:
    return run_db(lambda c: c.scalar(select(User.id).where(User.email == email)))


def _seed_doc(*, owner_id: uuid.UUID, status: str = "ready") -> uuid.UUID:
    doc_id = uuid.uuid4()
    run_db(lambda c: c.execute(Document.__table__.insert().values(
        id=doc_id, owner_id=owner_id, visibility="shared", source_system="upload",
        filename="Policy.pdf", file_format="pdf", storage_key=uuid.uuid4().hex,
        sha256="a" * 64, size_bytes=10, version=1, status=status, chunk_count=0,
        attempts=0, indexed_version=1 if status == "ready" else None,
        indexed_at=NOW if status == "ready" else None,
        created_at=NOW, updated_at=NOW,
    )))
    return doc_id


def _seed_chunk(doc_id: uuid.UUID, content: str, *, chunk_index: int = 1,
                created_at: datetime | None = None) -> int:
    values = {
        "document_id": doc_id, "chunk_index": chunk_index, "page_number": 1,
        "content": content, "embedding": b"",
    }
    if created_at is not None:
        values["created_at"] = created_at
    return run_db(lambda c: c.scalar(
        DocumentChunk.__table__.insert().values(**values).returning(DocumentChunk.id)
    ))


def _run_scan(**kwargs):
    async def _go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                return await scan_new_chunks(session, **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _candidates():
    return run_db(lambda c: c.execute(text(
        "SELECT left_chunk_id, right_chunk_id, status, detection_source "
        "FROM document_conflict_candidates ORDER BY detected_at"
    ))).all()


def _doc_row(doc_id: uuid.UUID):
    return run_db(lambda c: c.execute(text(
        "SELECT status, version FROM documents WHERE id = :d"
    ), {"d": doc_id})).one()


def test_ordered_pair_is_canonical():
    assert ordered_pair(3, 9) == (3, 9)
    assert ordered_pair(9, 3) == (3, 9)
    assert ordered_pair(5, 5) == (5, 5)


def test_scan_finds_contradictory_pair_and_changes_nothing():
    owner = _user_id(ADMIN_EMAIL)
    doc_a = _seed_doc(owner_id=owner)
    doc_b = _seed_doc(owner_id=owner)
    chunk_a = _seed_chunk(doc_a, KEEP)
    chunk_b = _seed_chunk(doc_b, DELETE)
    result = _run_scan(since=NOW - timedelta(hours=1))
    assert result.chunks_scanned == 2
    assert result.candidates_seen == 1
    assert result.candidates_created == 1
    rows = _candidates()
    assert len(rows) == 1
    assert (rows[0].left_chunk_id, rows[0].right_chunk_id) == (min(chunk_a, chunk_b), max(chunk_a, chunk_b))
    assert rows[0].status == "open"
    assert rows[0].detection_source == "nightly_scan"
    assert _doc_row(doc_a) == ("ready", 1)
    assert _doc_row(doc_b) == ("ready", 1)


def test_scan_is_idempotent():
    owner = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP)
    _seed_chunk(_seed_doc(owner_id=owner), DELETE)
    first = _run_scan(since=NOW - timedelta(hours=1))
    second = _run_scan(since=NOW - timedelta(hours=1))
    assert first.candidates_created == 1
    assert second.candidates_created == 0
    assert second.candidates_seen == 1
    assert len(_candidates()) == 1


def test_scan_ignores_identical_text():
    owner = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP)
    result = _run_scan(since=NOW - timedelta(hours=1))
    assert result.candidates_created == 0
    assert _candidates() == []


def test_scan_ignores_unrelated_passages():
    owner = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner), UNRELATED_A)
    _seed_chunk(_seed_doc(owner_id=owner), UNRELATED_B)
    result = _run_scan(since=NOW - timedelta(hours=1))
    assert result.candidates_created == 0
    assert _candidates() == []


def test_scan_respects_since_window():
    owner = _user_id(ADMIN_EMAIL)
    old = NOW - timedelta(days=2)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP, created_at=old)
    _seed_chunk(_seed_doc(owner_id=owner), DELETE, created_at=old)
    assert _run_scan(since=NOW - timedelta(hours=1)).candidates_created == 0
    assert _candidates() == []
    assert _run_scan(since=NOW - timedelta(days=3)).candidates_created == 1


def test_scan_finds_same_document_contradiction():
    owner = _user_id(ADMIN_EMAIL)
    doc = _seed_doc(owner_id=owner)
    _seed_chunk(doc, KEEP, chunk_index=1)
    _seed_chunk(doc, DELETE, chunk_index=2)
    result = _run_scan(since=NOW - timedelta(hours=1))
    assert result.candidates_created == 1
    assert len(_candidates()) == 1


def test_scan_skips_non_ready_documents():
    owner = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner, status="pending"), KEEP)
    _seed_chunk(_seed_doc(owner_id=owner, status="pending"), DELETE)
    result = _run_scan(since=NOW - timedelta(hours=1))
    assert result.chunks_scanned == 0
    assert result.candidates_created == 0
    assert _candidates() == []


def test_review_flow_records_decision_and_closes_candidate():
    owner = _user_id(ADMIN_EMAIL)
    reviewer = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP)
    _seed_chunk(_seed_doc(owner_id=owner), DELETE)
    _run_scan(since=NOW - timedelta(hours=1))
    candidate_id = run_db(lambda c: c.scalar(
        text("SELECT id FROM document_conflict_candidates")
    ))
    # Status cannot move without a recorded review.
    with pytest.raises(Exception, match="requires a recorded review event"):
        run_db(lambda c: c.execute(text(
            "UPDATE document_conflict_candidates SET status = 'confirmed' WHERE id = :c"
        ), {"c": candidate_id}))
    run_db(lambda c: c.execute(text(
        "INSERT INTO document_conflict_reviews (candidate_id, reviewer_user_id, decision, rationale) "
        "VALUES (:c, :u, 'confirmed', 'The two passages state opposite retention rules.')"
    ), {"c": candidate_id, "u": reviewer}))
    run_db(lambda c: c.execute(text(
        "UPDATE document_conflict_candidates SET status = 'confirmed' WHERE id = :c"
    ), {"c": candidate_id}))
    status = run_db(lambda c: c.scalar(text(
        "SELECT status FROM document_conflict_candidates WHERE id = :c"
    ), {"c": candidate_id}))
    assert status == "confirmed"


def test_migration_0105_roundtrip_and_downgrade_guard():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    assert {"document_conflict_candidates", "document_conflict_reviews"} <= _tables()
    try:
        command.downgrade(cfg, "0137_document_freshness")
        assert not ({"document_conflict_candidates", "document_conflict_reviews"} & _tables())
    finally:
        command.upgrade(cfg, "head")
    assert {"document_conflict_candidates", "document_conflict_reviews"} <= _tables()
    command.check(cfg)
    # A stored candidate blocks the downgrade (review history is kept).
    owner = _user_id(ADMIN_EMAIL)
    _seed_chunk(_seed_doc(owner_id=owner), KEEP)
    _seed_chunk(_seed_doc(owner_id=owner), DELETE)
    _run_scan(since=NOW - timedelta(hours=1))
    assert len(_candidates()) == 1
    try:
        command.downgrade(cfg, "0137_document_freshness")
    except Exception as exc:
        assert "review history exists" in str(exc)
    else:
        raise AssertionError("downgrade should have refused while candidates exist")
    finally:
        command.upgrade(cfg, "head")
    assert {"document_conflict_candidates", "document_conflict_reviews"} <= _tables()


def _tables() -> set[str]:
    rows = run_db(lambda c: c.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ))).all()
    return {row.table_name for row in rows}
