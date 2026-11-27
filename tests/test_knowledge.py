"""ADR-004 WP4: versioned organizational knowledge, passage provenance and the index outbox.

Drafts stay unpublished and out of RAG. Evidence is pinned to a document revision and becomes orphaned
when that revision changes or disappears. The outbox consumer retries, recovers and reconciles without
writing knowledge vectors. Runs against real PostgreSQL. All text is synthetic.
"""

import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import text

from app.main import app
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all, upload
from tests.test_access_control import new_group, set_access

POLICY = "Refund policy: customers may request a refund within 30 days of the invoice date by contacting billing."
TOKEN = "BLUEHERON-DRAFT-991"


def _rejects(sql: str, params: dict | None, match: str) -> None:
    with pytest.raises(Exception, match=match):
        run_db(lambda c: c.execute(text(sql), params or {}))


def chunk_row(document_id: str) -> tuple[str, int]:
    row = run_db(
        lambda c: c.execute(
            text(
                "SELECT content, id FROM document_chunks WHERE document_id = :d ORDER BY chunk_index LIMIT 1"
            ),
            {"d": document_id},
        )
    ).one()
    return row[0], row[1]


def quote_from(document_id: str) -> str:
    content, _ = chunk_row(document_id)
    return content[: min(80, len(content))]


def ready_doc(client, filename: str, body: str, visibility: str = "private") -> str:
    response = upload(client, filename, make_pdf(body), visibility=visibility)
    assert response.status_code == 202, response.text
    process_all(client)
    return response.json()["id"]


def draft(client, document_id: str, statement: str = "Customers have 30 days to ask for a refund.", **extra):
    body = {
        "statement": statement,
        "category": "policy",
        "visibility": "private",
        "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
    }
    body.update(extra)
    return client.post("/api/knowledge", json=body)


def drain(client, limit: int = 20) -> int:
    count = 0
    while count < limit and client.portal.call(app.state.rag.outbox.process_next):
        count += 1
    return count


def test_draft_records_provenance_and_is_not_indexed(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    document_id = ready_doc(client, "policy.pdf", POLICY)
    content, chunk_id = chunk_row(document_id)
    created = draft(client, document_id, f"The refund window is 30 days. {TOKEN}")
    assert created.status_code == 201, created.text
    body = created.json()
    revision = body["revisions"][0]
    assert revision["status"] == "draft" and revision["published"] is False
    assert revision["revision_number"] == 1
    evidence = revision["evidence"][0]
    assert evidence["quote"] == content[: min(80, len(content))]
    assert evidence["chunk_key"] == chunk_id
    assert evidence["document_version"] == 1
    assert evidence["orphan_reason"] is None
    assert content[evidence["start_offset"] : evidence["end_offset"]] == evidence["quote"]
    assert evidence["prefix"] == content[max(0, evidence["start_offset"] - 80) : evidence["start_offset"]]
    assert "author_user_id" not in body

    listed = client.get("/api/knowledge").json()["items"]
    assert [item["id"] for item in listed] == [body["id"]]
    assert listed[0]["current"]["published"] is False

    events = run_db(
        lambda c: c.execute(text("SELECT event_type, status FROM index_outbox ORDER BY id"))
    ).all()
    assert [row[0] for row in events] == ["knowledge.index"]
    assert drain(client) == 1
    done = run_db(lambda c: c.execute(text("SELECT status, result FROM index_outbox"))).one()
    assert tuple(done) == ("completed", "skipped_unpublished")
    assert app.state.rag.knowledge_store.count() == 0
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM knowledge_index_vectors"))) == 0
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM knowledge_claims"))) == 0
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM memory_items"))) == 0

    fake_provider.rag_reply = lambda message: "Customers may request a refund within 30 days [S1]."
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    answer = client.post(f"/api/conversations/{chat_id}/messages", json={"question": "How long is the refund window?"})
    assert answer.status_code == 200, answer.text
    prompt = "\n".join(message for _, message in fake_provider.calls)
    assert TOKEN not in prompt
    assert [c["filename"] for c in answer.json()["assistant_message"]["response"]["citations"]] == ["policy.pdf"]


def test_other_users_cannot_read_or_cite_someone_elses_draft(client):
    login(client, USER_EMAIL)
    document_id = ready_doc(client, "policy.pdf", POLICY)
    created = draft(client, document_id).json()
    missing = client.get(f"/api/knowledge/{uuid.uuid4()}")
    for email in (USER2_EMAIL, ADMIN_EMAIL):
        login(client, email)
        assert client.get("/api/knowledge").json()["items"] == []
        denied = client.get(f"/api/knowledge/{created['id']}")
        assert denied.status_code == 404 and denied.json() == missing.json()
        assert client.post(f"/api/knowledge/{created['id']}/withdraw").status_code == 404
        stolen = client.post(
            "/api/knowledge",
            json={
                "statement": "Taken from another user's private file.",
                "category": "policy",
                "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
            },
        )
        assert stolen.status_code == 422
    login(client, USER_EMAIL)
    assert client.get(f"/api/knowledge/{created['id']}").status_code == 200
    client.cookies.clear()
    assert client.get("/api/knowledge").status_code == 401


def test_offboarding_transfers_company_knowledge_but_keeps_private_drafts_private(client):
    login(client, ADMIN_EMAIL)
    group_id = new_group(client, "Offboarding Operations", kind="department")
    document_id = ready_doc(client, "offboarding-policy.pdf", POLICY)
    for email in (USER_EMAIL, USER2_EMAIL):
        assert client.post(
            f"/api/admin/access/groups/{group_id}/members", json={"email": email}
        ).status_code == 204
    assert set_access(client, document_id, "restricted", groups=[group_id]).status_code == 200

    login(client, USER_EMAIL)
    company_entry = draft(
        client,
        document_id,
        "The team follows the approved regional refund procedure.",
        visibility="restricted",
        scope_group_id=group_id,
        category="policy",
    )
    assert company_entry.status_code == 201, company_entry.text
    private_entry = draft(client, document_id, "A personal draft retained only for its author.")
    assert private_entry.status_code == 201, private_entry.text

    login(client, ADMIN_EMAIL)
    employee_id = run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": USER_EMAIL}))
    successor_id = str(run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": USER2_EMAIL})))
    company_id = company_entry.json()["id"]
    private_id = private_entry.json()["id"]

    refused = client.post(
        f"/api/admin/users/{employee_id}/active",
        json={"active": False, "knowledge_transfer_to": str(successor_id)},
    )
    assert refused.status_code == 403
    assert "data.steward" in refused.json()["error"]["message"]
    assert run_db(
        lambda c: c.scalar(text("SELECT owner_user_id FROM knowledge_entries WHERE id = :id"), {"id": company_id})
    ) == employee_id
    assert run_db(lambda c: c.scalar(text("SELECT is_active FROM users WHERE id = :id"), {"id": employee_id})) is True

    wrong_category = client.post(
        "/api/admin/access/grants",
        json={"email": USER2_EMAIL, "permission": "data.steward", "category": "financial", "scope_group_id": group_id},
    )
    assert wrong_category.status_code == 201, wrong_category.text
    assert client.post(
        f"/api/admin/users/{employee_id}/active",
        json={"active": False, "knowledge_transfer_to": successor_id},
    ).status_code == 403
    wrong_group_id = new_group(client, "Offboarding Other Scope")
    wrong_group = client.post(
        "/api/admin/access/grants",
        json={"email": USER2_EMAIL, "permission": "data.steward", "category": "policy", "scope_group_id": wrong_group_id},
    )
    assert wrong_group.status_code == 201, wrong_group.text
    assert client.post(
        f"/api/admin/users/{employee_id}/active",
        json={"active": False, "knowledge_transfer_to": successor_id},
    ).status_code == 403

    steward_grant = client.post(
        "/api/admin/access/grants",
        json={"email": USER2_EMAIL, "permission": "data.steward", "category": "policy", "scope_group_id": group_id},
    )
    assert steward_grant.status_code == 201, steward_grant.text
    deactivated = client.post(
        f"/api/admin/users/{employee_id}/active",
        json={"active": False, "knowledge_transfer_to": successor_id},
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False

    async def _read_owner_author(conn):
        result = await conn.execute(
            text("SELECT owner_user_id, author_user_id FROM knowledge_entries WHERE id = :id"), {"id": company_id}
        )
        return result.one()

    owner_author = run_db(_read_owner_author)
    assert tuple(owner_author) == (uuid.UUID(successor_id), employee_id)
    assert run_db(
        lambda c: c.scalar(text("SELECT author_user_id FROM knowledge_revisions WHERE entry_id = :id"), {"id": company_id})
    ) == employee_id
    assert run_db(
        lambda c: c.scalar(text("SELECT owner_user_id FROM knowledge_entries WHERE id = :id"), {"id": private_id})
    ) == employee_id

    login(client, USER2_EMAIL)
    assert {row["id"] for row in client.get("/api/knowledge").json()["items"]} == {company_id}
    assert client.get(f"/api/knowledge/{company_id}").status_code == 200
    assert client.get(f"/api/knowledge/{private_id}").status_code == 404
    other_revision = client.post(
        f"/api/knowledge/{company_id}/revisions",
        json={
            "statement": "The current steward confirmed the same source-backed refund procedure.",
            "category": "policy",
            "visibility": "restricted",
            "scope_group_id": group_id,
            "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
        },
    )
    assert other_revision.status_code == 200, other_revision.text
    assert run_db(
        lambda c: c.scalar(
            text("SELECT author_user_id FROM knowledge_revisions WHERE entry_id = :id ORDER BY revision_number DESC LIMIT 1"),
            {"id": company_id},
        )
    ) == uuid.UUID(successor_id)


def test_admin_can_recover_company_knowledge_from_an_already_inactive_account(client):
    login(client, ADMIN_EMAIL)
    group_id = new_group(client, "Legacy Offboarding", kind="department")
    document_id = ready_doc(client, "legacy-handoff-policy.pdf", POLICY)
    assert client.post(
        f"/api/admin/access/groups/{group_id}/members", json={"email": USER_EMAIL}
    ).status_code == 204
    assert client.post(
        f"/api/admin/access/groups/{group_id}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    users = {row["email"]: row["id"] for row in client.get("/api/admin/users").json()["users"]}
    assert client.patch(
        f"/api/admin/users/{users[USER2_EMAIL]}",
        json={"role": "employee", "department_ids": [group_id]},
    ).status_code == 200
    assert set_access(client, document_id, "restricted", groups=[group_id]).status_code == 200

    login(client, USER_EMAIL)
    created = draft(
        client,
        document_id,
        "A source-backed procedure whose author has already left.",
        visibility="restricted",
        scope_group_id=group_id,
        category="policy",
    )
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]
    employee_id = run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": USER_EMAIL}))
    successor_id = str(run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": USER2_EMAIL})))
    run_db(lambda c: c.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": employee_id}))

    login(client, ADMIN_EMAIL)
    denied = client.post(
        f"/api/admin/users/{employee_id}/knowledge-transfer",
        json={"successor_user_id": str(successor_id)},
    )
    assert denied.status_code == 403
    template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Legacy knowledge steward",
            "base_role": "employee",
            "permissions": [{"permission": "data.steward", "categories": ["policy"]}],
        },
    )
    assert template.status_code == 201, template.text
    assignment = client.post(
        f"/api/admin/role-templates/{template.json()['id']}/assignments",
        json={
            "email": USER2_EMAIL,
            "department_ids": [group_id],
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
    )
    assert assignment.status_code == 201, assignment.text
    transferred = client.post(
        f"/api/admin/users/{employee_id}/knowledge-transfer",
        json={"successor_user_id": str(successor_id)},
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json() == {"transferred_entries": 1}
    login(client, USER2_EMAIL)
    assert client.get(f"/api/knowledge/{entry_id}").status_code == 200
    assert run_db(
        lambda c: c.scalar(text("SELECT owner_user_id FROM knowledge_entries WHERE id = :id"), {"id": entry_id})
    ) == uuid.UUID(successor_id)


def test_visibility_cannot_exceed_source_acl(client):
    login(client, ADMIN_EMAIL)
    private_id = ready_doc(client, "private.pdf", POLICY)
    widened = draft(client, private_id, visibility="shared")
    assert widened.status_code == 422

    shared_id = ready_doc(client, "shared.pdf", POLICY, visibility="shared")
    assert draft(client, shared_id, visibility="shared").status_code == 201

    group = new_group(client, "Billing")
    other = new_group(client, "Audit")
    restricted_id = ready_doc(client, "restricted.pdf", POLICY)
    assert set_access(client, restricted_id, "restricted", groups=[group]).status_code == 200
    in_scope = draft(client, restricted_id, visibility="restricted", scope_group_id=group)
    assert in_scope.status_code == 201, in_scope.text
    too_wide = draft(client, restricted_id, visibility="restricted", scope_group_id=other)
    assert too_wide.status_code == 422
    assert draft(client, restricted_id, visibility="shared").status_code == 422

    login(client, USER_EMAIL)
    own = ready_doc(client, "mine.pdf", POLICY)
    assert draft(client, own, visibility="shared").status_code == 403


def test_revisions_are_immutable_and_publication_requires_approval(client):
    login(client, USER_EMAIL)
    document_id = ready_doc(client, "policy.pdf", POLICY)
    created = draft(client, document_id).json()
    revision_id = created["revisions"][0]["id"]
    author = run_db(lambda c: c.scalar(text("SELECT author_user_id FROM knowledge_entries WHERE id = :i"), {"i": created["id"]}))

    _rejects("UPDATE knowledge_revisions SET statement = 'rewritten' WHERE id = :id", {"id": revision_id}, "immutable")
    _rejects("UPDATE knowledge_revisions SET status = 'published' WHERE id = :id", {"id": revision_id}, "cannot be published")
    _rejects("DELETE FROM knowledge_revisions WHERE id = :id", {"id": revision_id}, "never deleted")
    _rejects(
        "UPDATE knowledge_evidence SET quote = 'forged' WHERE revision_id = :id",
        {"id": revision_id},
        "immutable",
    )
    _rejects(
        "INSERT INTO knowledge_revisions (id, entry_id, author_user_id, revision_number, statement, category, "
        "visibility, status) VALUES (:id, :entry, :author, 2, 'published text', 'policy', 'private', 'published')",
        {"id": str(uuid.uuid4()), "entry": created["id"], "author": author},
        "cannot be published",
    )
    _rejects(
        "INSERT INTO knowledge_index_vectors (revision_id, embedding) VALUES (:id, :e)",
        {"id": revision_id, "e": b"\x00" * 16},
        "unpublished knowledge cannot be indexed",
    )
    _rejects("DELETE FROM index_outbox", {}, "never deleted")
    _rejects("DELETE FROM knowledge_evidence", {}, "never deleted")

    revised = client.post(
        f"/api/knowledge/{created['id']}/revisions",
        json={
            "statement": "The refund window is 30 days from the invoice, not from delivery.",
            "category": "policy",
            "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
        },
    )
    assert revised.status_code == 200, revised.text
    statuses = [(row["revision_number"], row["status"]) for row in revised.json()["revisions"]]
    assert statuses == [(1, "superseded"), (2, "draft")]
    assert revised.json()["revisions"][0]["statement"].startswith("Customers have 30 days")
    withdrawn = client.post(f"/api/knowledge/{created['id']}/withdraw")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["revisions"][-1]["status"] == "withdrawn"
    assert client.post(f"/api/knowledge/{created['id']}/withdraw").status_code == 409


def test_department_knowledge_requires_two_people_and_is_acl_filtered_in_rag(client, fake_provider):
    from tests.test_access_control import user_id

    activate_provider(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "department-policy.pdf", POLICY)
    group = new_group(client, "Billing", kind="department")
    assert set_access(client, document_id, "restricted", groups=[group]).status_code == 200
    for email in (USER_EMAIL, USER2_EMAIL):
        assert client.post(f"/api/admin/access/groups/{group}/members", json={"email": email}).status_code == 204

    token = "FJORDORCHID991"
    created = draft(
        client,
        document_id,
        f"{token} is the approved department service window.",
    )
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]
    private = client.post(f"/api/knowledge/{entry_id}/submit")
    assert private.status_code == 422

    # Create a second department scoped statement that can be submitted for approval.
    source_text, _ = chunk_row(document_id)
    scoped = client.post(
        "/api/knowledge",
        json={
            "statement": f"{token} is the approved department service window.",
            "category": "policy",
            "visibility": "restricted",
            "scope_group_id": group,
            "evidence": [{"document_id": document_id, "quote": source_text[:80]}],
        },
    )
    assert scoped.status_code == 201, scoped.text
    entry_id = scoped.json()["id"]
    revision_id = scoped.json()["revisions"][0]["id"]
    submitted = client.post(f"/api/knowledge/{entry_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["revisions"][0]["status"] == "submitted"

    for email, permission in ((USER_EMAIL, "knowledge.review"), (USER2_EMAIL, "knowledge.publish")):
        grant = client.post(
            "/api/admin/access/grants",
            json={"email": email, "permission": permission, "category": "policy", "scope_group_id": group},
        )
        assert grant.status_code == 201, grant.text

    login(client, USER2_EMAIL)
    publish_first = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "publish", "rationale": "This must not precede review."},
    )
    assert publish_first.status_code == 409

    login(client, USER_EMAIL)
    review_queue = client.get("/api/governance/knowledge").json()["items"]
    assert [item["id"] for item in review_queue] == [revision_id]
    assert review_queue[0]["sources"][0]["filename"] == "department-policy.pdf"
    reviewed = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "review", "rationale": "Source supports the department operating rule."},
    )
    assert reviewed.status_code == 200, reviewed.text

    login(client, USER2_EMAIL)
    publish_queue = client.get("/api/governance/knowledge").json()["items"]
    assert publish_queue[0]["allowed_stages"] == ["publish"]
    published = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "publish", "rationale": "Approved for scoped operational use."},
    )
    assert published.status_code == 200, published.text
    login(client, ADMIN_EMAIL)
    approved = client.get(f"/api/knowledge/{entry_id}").json()["revisions"][0]
    assert approved["status"] == "published" and approved["published"] is True

    login(client, USER2_EMAIL)
    fake_provider.rag_reply = lambda message: f"The approved department service window is {token}. [S1]" if token in message else "INSUFFICIENT_EVIDENCE"
    response = client.post("/api/rag/chat", json={"question": "What is the approved department service window?"})
    assert response.status_code == 200, response.text
    assert response.json()["retrieved_chunks"] >= 1
    assert token in "\n".join(message for _, message in fake_provider.calls)
    assert response.json()["citations"][0]["filename"] == "department-policy.pdf"

    # A reviewer without department membership cannot see the submission or its evidence.
    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/admin/access/groups/{group}/members/{user_id(USER2_EMAIL)}").status_code == 204
    login(client, USER2_EMAIL)
    assert client.get("/api/governance/knowledge").json()["items"] == []
    fake_provider.calls.clear()
    denied = client.post("/api/rag/chat", json={"question": "What is the approved department service window?"})
    assert denied.status_code == 200, denied.text
    assert all(token not in message for _, message in fake_provider.calls)


def test_document_replacement_and_deletion_orphan_evidence_without_indexing(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    document_id = ready_doc(client, "policy.pdf", POLICY)
    created = draft(client, document_id, f"Refunds take 30 days. {TOKEN}")
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]
    before_vectors = set(app.state.rag.vector_store.ids())

    replaced = client.put(
        f"/api/rag/documents/{document_id}/file",
        files={"file": ("policy.pdf", make_pdf("This replacement talks about office hours only."), "application/pdf")},
    )
    assert replaced.status_code == 202, replaced.text
    evidence = client.get(f"/api/knowledge/{entry_id}").json()["revisions"][0]["evidence"][0]
    assert evidence["orphan_reason"] == "document_revision_changed"
    assert evidence["document_id"] == document_id  # the row still exists; the cited revision does not
    lag = draft(client, document_id, "Office hours are nine to five.")
    assert lag.status_code == 409  # indexed text has not caught up with the new file

    fake_provider.rag_reply = lambda message: "Customers may request a refund within 30 days [S1]."
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    answer = client.post(f"/api/conversations/{chat_id}/messages", json={"question": "How long is the refund window?"})
    assert answer.status_code == 200, answer.text
    prompt = "\n".join(message for _, message in fake_provider.calls)
    assert TOKEN not in prompt
    assert "policy.pdf" in answer.text  # the previous indexed revision is still searchable
    assert set(app.state.rag.vector_store.ids()) == before_vectors

    process_all(client)
    assert client.get(f"/api/knowledge/{entry_id}").json()["revisions"][0]["evidence"][0]["orphan_reason"] == "document_revision_changed"
    assert app.state.rag.knowledge_store.count() == 0

    login(client, USER_EMAIL)
    other = ready_doc(client, "notes.pdf", POLICY)
    second = draft(client, other, "A second draft citing notes.")
    assert second.status_code == 201, second.text
    assert client.delete(f"/api/rag/documents/{other}").status_code == 204
    deleted = client.get(f"/api/knowledge/{second.json()['id']}").json()["revisions"][0]["evidence"][0]
    assert deleted["orphan_reason"] == "source_deleted"
    assert deleted["document_id"] is None and deleted["document_key"] == other
    assert "refund" in deleted["quote"].lower() or deleted["quote"]  # the captured passage remains
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM knowledge_evidence WHERE document_key = :d"), {"d": other})) == 1


def test_outbox_retries_recovers_and_reconciles(client):
    login(client, USER_EMAIL)
    document_id = ready_doc(client, "policy.pdf", POLICY)
    created = draft(client, document_id)
    assert created.status_code == 201, created.text
    worker = app.state.rag.outbox
    worker.fail_next = 3
    assert client.portal.call(worker.process_next) is True
    assert client.portal.call(worker.process_next) is True
    assert client.portal.call(worker.process_next) is True
    failed = run_db(lambda c: c.execute(text("SELECT status, attempts FROM index_outbox ORDER BY id"))).all()
    assert failed[0][0] == "failed" and failed[0][1] == 3
    assert client.portal.call(worker.recover_failed) == 1
    assert client.portal.call(worker.process_next) is True
    assert run_db(lambda c: c.execute(text("SELECT status, result FROM index_outbox WHERE event_type = 'knowledge.index'"))).one() == (
        "completed",
        "skipped_unpublished",
    )
    assert app.state.rag.knowledge_store.count() == 0

    # A crash after the claim leaves the event processing; a later claim finishes it once.
    run_db(
        lambda c: c.execute(
            text(
                "INSERT INTO index_outbox (idempotency_key, event_type, aggregate_id) "
                "VALUES ('knowledge.index.replay', 'knowledge.index', :id)"
            ),
            {"id": created.json()["revisions"][0]["id"]},
        )
    )
    run_db(lambda c: c.execute(text("UPDATE index_outbox SET status = 'processing', attempts = 1, locked_until = now() - interval '2 minutes' WHERE idempotency_key = 'knowledge.index.replay'")))
    assert client.portal.call(worker.process_next) is True
    replay = run_db(
        lambda c: c.execute(
            text("SELECT status, result, attempts FROM index_outbox WHERE idempotency_key = 'knowledge.index.replay'")
        )
    ).one()
    assert tuple(replay) == ("completed", "skipped_unpublished", 2)
    _rejects(
        "INSERT INTO index_outbox (idempotency_key, event_type, aggregate_id) "
        "VALUES ('knowledge.index.replay', 'knowledge.index', :id)",
        {"id": created.json()["revisions"][0]["id"]},
        "uq_index_outbox_idempotency_key|duplicate key",
    )

    stray = np.zeros((1, app.state.rag.knowledge_store.dimension), dtype=np.float32)
    app.state.rag.knowledge_store.apply(add_ids=[99], vectors=stray)
    assert app.state.rag.knowledge_store.count() == 1
    document_ids = set(app.state.rag.vector_store.ids())
    result = client.portal.call(worker.reconcile)
    assert result["vectors"] == 0 and app.state.rag.knowledge_store.count() == 0
    assert set(app.state.rag.vector_store.ids()) == document_ids
