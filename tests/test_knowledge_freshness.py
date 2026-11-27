"""PostgreSQL acceptance for annual organizational-knowledge freshness reviews."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db.models import KnowledgeRevision, User
from app.main import app
from app.rag.retrieval import retrieve_approved_knowledge
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_knowledge import POLICY, quote_from, ready_doc

STATEMENT = "The customer support retention period is eighteen months under policy ORCHID-FRESHNESS-31."
QUESTION = "What is the ORCHID-FRESHNESS-31 customer support retention period?"


def _retrieve_for(email: str):
    async def _run():
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            assert user is not None
            return await retrieve_approved_knowledge(db, user, QUESTION, top_k=3)

    return _run


def _backdate_due(revision_id: str) -> None:
    async def _run(conn):
        await conn.execute(text("ALTER TABLE knowledge_revisions DISABLE TRIGGER knowledge_revision_freshness_fields_guard"))
        try:
            await conn.execute(
                text("UPDATE knowledge_revisions SET review_due_at = now() - interval '1 day' WHERE id = :id"),
                {"id": revision_id},
            )
        finally:
            await conn.execute(text("ALTER TABLE knowledge_revisions ENABLE TRIGGER knowledge_revision_freshness_fields_guard"))

    run_db(_run)


def test_overdue_approved_knowledge_is_withheld_until_scoped_freshness_review(client):
    login(client, ADMIN_EMAIL)
    source_id = ready_doc(client, "freshness-policy.pdf", POLICY + " Support retention is eighteen months.", visibility="shared")
    created = client.post(
        "/api/knowledge",
        json={
            "statement": STATEMENT,
            "category": "policy",
            "visibility": "shared",
            "evidence": [{"document_id": source_id, "quote": quote_from(source_id)}],
        },
    )
    assert created.status_code == 201, created.text
    revision_id = created.json()["revisions"][0]["id"]
    entry_id = created.json()["id"]
    assert client.post(f"/api/knowledge/{entry_id}/submit").status_code == 200

    assert client.post(
        "/api/admin/access/grants",
        json={"email": USER_EMAIL, "permission": "knowledge.review", "category": "policy"},
    ).status_code == 201
    assert client.post(
        "/api/admin/access/grants",
        json={"email": USER2_EMAIL, "permission": "knowledge.publish", "category": "policy"},
    ).status_code == 201

    login(client, USER_EMAIL)
    assert client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "review", "rationale": "Checked the source text and policy scope."},
    ).status_code == 200
    login(client, USER2_EMAIL)
    assert client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "publish", "rationale": "Approved the reviewed policy for shared use."},
    ).status_code == 200

    async def _deadline(conn):
        result = await conn.execute(
            text("SELECT last_reviewed_at, review_due_at FROM knowledge_revisions WHERE id = :id"),
            {"id": revision_id},
        )
        return result.one()

    last_reviewed, due_at = run_db(_deadline)
    assert last_reviewed is not None and due_at - last_reviewed == timedelta(days=365)

    # This test-only backdate uses a disposable *_test database. The application cannot edit deadlines directly.
    _backdate_due(revision_id)
    login(client, USER_EMAIL)
    stale = client.get("/api/governance/knowledge/stale")
    assert stale.status_code == 200, stale.text
    assert [item["id"] for item in stale.json()["items"]] == [revision_id]
    assert stale.json()["items"][0]["sources"][0]["filename"] == "freshness-policy.pdf"
    assert client.post(
        f"/api/governance/knowledge/{revision_id}/freshness-review", json={"rationale": "ok"}
    ).status_code == 422

    before_review = client.portal.call(_retrieve_for(USER_EMAIL))
    assert before_review == []

    login(client, USER2_EMAIL)
    assert client.get("/api/governance/knowledge/stale").json()["items"] == []
    denied = client.post(
        f"/api/governance/knowledge/{revision_id}/freshness-review",
        json={"rationale": "This user has a publish grant but no freshness review grant."},
    )
    assert denied.status_code == 403

    login(client, USER_EMAIL)
    reviewed = client.post(
        f"/api/governance/knowledge/{revision_id}/freshness-review",
        json={"rationale": "Confirmed the cited procedure still supports the current retention period."},
    )
    assert reviewed.status_code == 200, reviewed.text
    result = reviewed.json()
    assert result["revision_id"] == revision_id
    assert result["reviewer_email"] == USER_EMAIL
    assert datetime.fromisoformat(result["next_review_due_at"].replace("Z", "+00:00")) > datetime.now(UTC)
    assert client.get("/api/governance/knowledge/stale").json()["items"] == []
    assert len(client.portal.call(_retrieve_for(USER_EMAIL))) == 1

    count = run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM knowledge_freshness_reviews WHERE revision_id = :id"), {"id": revision_id}
        )
    )
    assert count == 1
    with pytest.raises(Exception, match="authorized review event"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE knowledge_revisions SET review_due_at = now() + interval '1000 days' WHERE id = :id"),
                {"id": revision_id},
            )
        )
    with pytest.raises(Exception, match="append-only"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE knowledge_freshness_reviews SET rationale = 'edited' WHERE revision_id = :id"),
                {"id": revision_id},
            )
        )
