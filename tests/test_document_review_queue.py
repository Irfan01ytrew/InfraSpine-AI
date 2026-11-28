"""Step AI slice 5 tests: review queue + owner-set freshness dates.

Runs against a dedicated *_test PostgreSQL database (see tests/conftest.py).
The queue only ever lists documents the caller may already read, and the
PATCH endpoint only lets a document's owner change its review metadata.
Freshness is advisory: queueing a document never hides it or changes
what is cited.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Document, DocumentChunk, DocumentConflictCandidate, User
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, login, run_db

NOW = datetime.now(timezone.utc)


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


def _seed_chunk(doc_id: uuid.UUID, *, chunk_index: int = 1) -> int:
    return run_db(lambda c: c.scalar(
        DocumentChunk.__table__.insert().values(
            document_id=doc_id, chunk_index=chunk_index, page_number=1,
            content="The policy says keep the record.", embedding=b"",
        ).returning(DocumentChunk.id)
    ))


def _seed_open_candidate(left: int, right: int) -> None:
    lo, hi = min(left, right), max(left, right)
    run_db(lambda c: c.execute(DocumentConflictCandidate.__table__.insert().values(
        left_chunk_id=lo, right_chunk_id=hi, status="open",
        detection_source="nightly_scan", detected_at=NOW,
    )))


def _as_dt(value: str) -> datetime:
    """Parse an API datetime string (Pydantic renders UTC with a 'Z')."""
    return datetime.fromisoformat(value)


def _queue(client) -> list[dict]:
    resp = client.get("/api/rag/documents/review-queue")
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def test_review_queue_orders_expired_before_stale_and_excludes_fresh(admin_client):
    owner = _user_id(ADMIN_EMAIL)
    fresh = _seed_doc(owner_id=owner, filename="Fresh.pdf")
    stale = _seed_doc(
        owner_id=owner, filename="Stale.pdf",
        created_at=NOW - timedelta(days=400), updated_at=NOW - timedelta(days=400),
        indexed_at=NOW - timedelta(days=400),
    )
    expired = _seed_doc(owner_id=owner, filename="Expired.pdf", expires_at=NOW - timedelta(days=1))
    # The queue is global; only assert about the documents this test seeded.
    entries = [e for e in _queue(admin_client)
               if e["document"]["id"] in {str(fresh), str(stale), str(expired)}]
    ids = [e["document"]["id"] for e in entries]
    assert ids == [str(expired), str(stale)]
    assert str(fresh) not in ids
    assert entries[0]["severity"] == 0
    assert entries[0]["document"]["freshness_status"] == "expired"
    assert entries[1]["severity"] == 2
    assert entries[1]["document"]["freshness_status"] == "stale"
    assert entries[0]["open_conflicts"] == 0


def test_review_queue_includes_fresh_doc_with_open_conflicts(admin_client):
    owner = _user_id(ADMIN_EMAIL)
    doc_a = _seed_doc(owner_id=owner, filename="A.pdf")
    doc_b = _seed_doc(owner_id=owner, filename="B.pdf")
    _seed_open_candidate(_seed_chunk(doc_a), _seed_chunk(doc_b))
    entries = [e for e in _queue(admin_client)
               if e["document"]["id"] in {str(doc_a), str(doc_b)}]
    by_id = {e["document"]["id"]: e for e in entries}
    assert by_id[str(doc_a)]["open_conflicts"] == 1
    assert by_id[str(doc_b)]["open_conflicts"] == 1
    # Conflicts alone are the lowest severity; the documents stay fresh.
    assert by_id[str(doc_a)]["severity"] == 3
    assert by_id[str(doc_a)]["document"]["freshness_status"] == "fresh"


def test_review_queue_never_lists_unreadable_documents(client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL), visibility="private",
                       expires_at=NOW - timedelta(days=1))
    login(client, USER2_EMAIL)
    assert str(doc_id) not in {e["document"]["id"] for e in _queue(client)}
    assert client.get(f"/api/rag/documents/{doc_id}").status_code == 404


def test_patch_freshness_sets_and_clears_review_date(admin_client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL))
    due = NOW + timedelta(days=90)
    resp = admin_client.patch(f"/api/rag/documents/{doc_id}/freshness", json={"review_date": due.isoformat()})
    assert resp.status_code == 200, resp.text
    assert _as_dt(resp.json()["review_date"]) == due
    resp = admin_client.patch(f"/api/rag/documents/{doc_id}/freshness", json={"review_date": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_date"] is None


def test_patch_freshness_leaves_unset_fields_alone(admin_client):
    owner = _user_id(ADMIN_EMAIL)
    expires = NOW + timedelta(days=30)
    doc_id = _seed_doc(owner_id=owner, expires_at=expires)
    due = NOW + timedelta(days=90)
    resp = admin_client.patch(f"/api/rag/documents/{doc_id}/freshness", json={"review_date": due.isoformat()})
    assert resp.status_code == 200, resp.text
    assert _as_dt(resp.json()["review_date"]) == due
    assert _as_dt(resp.json()["expires_at"]) == expires


def test_patch_freshness_superseded_by_must_be_another_readable_document(admin_client):
    owner = _user_id(ADMIN_EMAIL)
    replacement = _seed_doc(owner_id=owner, filename="Policy v2.pdf")
    old = _seed_doc(owner_id=owner, filename="Policy.pdf")
    # A document cannot supersede itself.
    assert admin_client.patch(
        f"/api/rag/documents/{old}/freshness", json={"superseded_by": str(old)}
    ).status_code == 422
    # The replacement must exist and be readable.
    assert admin_client.patch(
        f"/api/rag/documents/{old}/freshness", json={"superseded_by": str(uuid.uuid4())}
    ).status_code == 404
    resp = admin_client.patch(
        f"/api/rag/documents/{old}/freshness", json={"superseded_by": str(replacement)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["superseded_by"] == str(replacement)
    assert resp.json()["freshness_status"] == "superseded"


def test_patch_freshness_requires_owner(client):
    doc_id = _seed_doc(owner_id=_user_id(ADMIN_EMAIL))
    login(client, USER2_EMAIL)
    resp = client.patch(
        f"/api/rag/documents/{doc_id}/freshness",
        json={"review_date": (NOW + timedelta(days=90)).isoformat()},
    )
    assert resp.status_code == 403
