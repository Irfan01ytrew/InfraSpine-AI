"""Step AI slice 2 tests: freshness metadata migration + freshness on read.

Runs against a dedicated *_test PostgreSQL database (see tests/conftest.py).
Freshness is advisory: it is computed on read, never stored, and shown only for
documents the caller may already read (`readable_by` + `documents.read`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.db.models import Document, User
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, login, run_db

NOW = datetime.now(timezone.utc)


def _columns() -> set[str]:
    rows = run_db(lambda c: c.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'documents'"
    ))).all()
    return {row.column_name for row in rows}


def test_migration_0104_roundtrip_and_no_autogenerate_drift():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    assert {"review_date", "expires_at", "superseded_by"} <= _columns()
    try:
        command.downgrade(cfg, "0103_unified_studio_governance")
        assert not ({"review_date", "expires_at", "superseded_by"} & _columns())
    finally:
        command.upgrade(cfg, "head")
    assert {"review_date", "expires_at", "superseded_by"} <= _columns()
    # Models and migrations must agree exactly (types, constraints, indexes).
    command.check(cfg)


def _user_id(email: str) -> uuid.UUID:
    return run_db(lambda c: c.scalar(select(User.id).where(User.email == email)))


def _seed_doc(*, owner_id: uuid.UUID, filename: str = "Policy.pdf", **overrides) -> uuid.UUID:
    doc_id = uuid.uuid4()
    values = {
        "id": doc_id,
        "owner_id": owner_id,
        "visibility": "shared",
        "source_system": "upload",
        "filename": filename,
        "file_format": "pdf",
        "storage_key": uuid.uuid4().hex,
        "sha256": "a" * 64,
        "size_bytes": 10,
        "version": 1,
        "status": "ready",
        "chunk_count": 0,
        "attempts": 0,
        "indexed_version": 1,
        "indexed_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    run_db(lambda c: c.execute(Document.__table__.insert().values(**values)))
    return doc_id


def _listed(client, doc_id: uuid.UUID) -> dict:
    docs = client.get("/api/rag/documents").json()["documents"]
    matches = [d for d in docs if d["id"] == str(doc_id)]
    assert len(matches) == 1, [d["id"] for d in docs]
    return matches[0]


def test_fresh_document_shows_freshness_on_list_and_detail(admin_client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL))
    listed = _listed(admin_client, doc_id)
    assert listed["freshness_status"] == "fresh"
    assert listed["freshness_score"] == 100
    assert listed["freshness_reasons"] == []
    assert listed["review_date"] is None
    assert listed["expires_at"] is None
    assert listed["superseded_by"] is None
    detail = admin_client.get(f"/api/rag/documents/{doc_id}").json()
    assert detail["freshness_status"] == "fresh"
    assert detail["freshness_score"] == 100


def test_expired_document_is_flagged_on_read(admin_client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL), expires_at=NOW - timedelta(days=1))
    listed = _listed(admin_client, doc_id)
    assert listed["freshness_status"] == "expired"
    assert listed["freshness_score"] == 0
    assert any("expired" in reason for reason in listed["freshness_reasons"])


def test_superseded_document_is_flagged_on_read(admin_client):
    owner = _user_id(ADMIN_EMAIL)
    replacement = _seed_doc(owner_id=owner, filename="Policy v2.pdf")
    old = _seed_doc(owner_id=owner, filename="Policy.pdf", superseded_by=replacement)
    listed = _listed(admin_client, old)
    assert listed["freshness_status"] == "superseded"
    assert listed["freshness_score"] == 0
    assert listed["superseded_by"] == str(replacement)


def test_overdue_review_is_aging_and_names_the_date(admin_client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL), review_date=NOW - timedelta(days=30))
    listed = _listed(admin_client, doc_id)
    assert listed["freshness_status"] == "aging"
    assert listed["freshness_score"] == 60
    assert any("Review was due" in reason for reason in listed["freshness_reasons"])


def test_freshness_never_leaks_unreadable_documents(client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL), visibility="private")
    login(client, USER2_EMAIL)
    docs = client.get("/api/rag/documents").json()["documents"]
    assert doc_id not in {d["id"] for d in docs}
    assert client.get(f"/api/rag/documents/{doc_id}").status_code == 404


def test_downgrade_refuses_when_freshness_metadata_exists():
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL), expires_at=NOW + timedelta(days=30))
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    try:
        command.downgrade(cfg, "0103_unified_studio_governance")
    except Exception as exc:
        assert "freshness metadata exists" in str(exc)
    else:
        raise AssertionError("downgrade should have refused while metadata exists")
    finally:
        command.upgrade(cfg, "head")
    assert {"review_date", "expires_at", "superseded_by"} <= _columns()
    # Clean up the metadata row so later tests start from a clean slate.
    run_db(lambda c: c.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc_id}))
