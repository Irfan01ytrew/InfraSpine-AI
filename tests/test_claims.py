"""ADR-004 WP3: unverified claims capture. Claimant identity, ownership-aware access, withdrawal,
immutable history, forged claimant/approval attempts, and isolation from approved knowledge, RAG
and personal memory. Runs against real PostgreSQL. All statements and people are synthetic."""

import uuid

import pytest
from sqlalchemy import text

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import User
from app.main import app
from app.services import access_control
from app.services.audit import AuditContext
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all, upload
from tests.test_access_control import APPROVAL, CLAIM, GRANT, in_app

CTX = AuditContext(None, "reviewer@test", None, None)
STATEMENT = "The company refund window is 30 days from the invoice date."


def submit(client, statement: str = STATEMENT, **extra):
    body = {"kind": "proposal", "statement": statement, "category": "policy"} | extra
    return client.post("/api/claims", json=body)


def user_id(email: str) -> uuid.UUID:
    return run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email}))


def _rejects(sql: str, params: dict, match: str) -> None:
    with pytest.raises(Exception, match=match):
        run_db(lambda c: c.execute(text(sql), params))


def test_submit_list_get_and_withdraw(client):
    login(client, USER_EMAIL)
    created = submit(client, sources=[{"kind": "note", "note": "From the 2024 billing memo."}])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "submitted" and body["verified"] is False
    assert body["kind"] == "proposal" and body["category"] == "policy"
    assert "claimant_user_id" not in body  # identity is the session, never a writable field
    assert body["sources"] == [{"kind": "note", "note": "From the 2024 billing memo."}]
    assert body["withdrawn_at"] is None

    listed = client.get("/api/claims")
    assert listed.status_code == 200 and [c["id"] for c in listed.json()["items"]] == [body["id"]]

    detail = client.get(f"/api/claims/{body['id']}")
    assert detail.status_code == 200
    history = detail.json()["history"]
    assert [(e["action"], e["from_status"], e["to_status"]) for e in history] == [("submitted", None, "submitted")]

    withdrawn = client.post(f"/api/claims/{body['id']}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["withdrawn_at"] and withdrawn.json()["verified"] is False
    assert client.post(f"/api/claims/{body['id']}/withdraw").status_code == 409

    after = client.get(f"/api/claims/{body['id']}").json()
    assert [e["action"] for e in after["history"]] == ["submitted", "withdrawn"]
    actions = [r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'claim.%'"))).all()]
    assert actions == ["claim.submitted", "claim.withdrawn"]
    details = run_db(lambda c: c.execute(text("SELECT details::text FROM audit_logs WHERE action LIKE 'claim.%'"))).all()
    assert all("30 days" not in d[0] and "billing memo" not in d[0] for d in details)


def test_validation_rejects_empty_unknown_and_forged_claimant_fields(client):
    login(client, USER_EMAIL)
    assert submit(client, "   ").status_code == 422
    assert submit(client, "x" * 4001).status_code == 422
    assert submit(client, kind="rumour").status_code == 422
    assert submit(client, category="secret").status_code == 422
    forged = client.post(
        "/api/claims",
        json={
            "kind": "proposal",
            "statement": STATEMENT,
            "category": "policy",
            "claimant_user_id": str(user_id(USER2_EMAIL)),
        },
    )
    assert forged.status_code == 422  # extra field: identity is never taken from the body
    assert submit(client, sources=[{"kind": "url", "url": 'https' + '://' + 'user:pass' + '@' + 'example.com/policy'}]).status_code == 422
    assert submit(client, sources=[{"kind": "note", "note": ""}]).status_code == 422


def test_claims_are_private_to_the_claimant(client):
    login(client, USER_EMAIL)
    claim = submit(client).json()
    missing = client.get(f"/api/claims/{uuid.uuid4()}")
    for email in (USER2_EMAIL, ADMIN_EMAIL):
        login(client, email)
        assert client.get("/api/claims").json()["items"] == []
        denied = client.get(f"/api/claims/{claim['id']}")
        assert denied.status_code == 404 and denied.json() == missing.json()
        assert client.post(f"/api/claims/{claim['id']}/withdraw").status_code == 404
        other = submit(client, "Our travel policy allows economy class only.")
        assert other.status_code == 201
    login(client, USER_EMAIL)
    own = client.get("/api/claims").json()["items"]
    assert [c["statement"] for c in own] == [STATEMENT]
    client.cookies.clear()
    assert client.get("/api/claims").status_code == 401


def test_document_sources_require_read_access_and_scope_requires_membership(client):
    login(client, ADMIN_EMAIL)
    group = client.post("/api/admin/access/groups", json={"name": "Billing", "kind": "department"}).json()
    private = upload(client, "internal.pdf", make_pdf("Internal only refund exception.")).json()["id"]
    login(client, USER_EMAIL)
    shared = upload(client, "handbook.pdf", make_pdf("The public handbook mentions leave policy.")).json()["id"]

    hidden = submit(client, sources=[{"kind": "document", "document_id": private}])
    assert hidden.status_code == 422
    scoped = submit(client, scope_group_id=group["id"])
    assert scoped.status_code == 422

    login(client, ADMIN_EMAIL)
    client.post(f"/api/admin/access/groups/{group['id']}/members", json={"email": USER_EMAIL})
    login(client, USER_EMAIL)
    ok = submit(
        client,
        scope_group_id=group["id"],
        sources=[{"kind": "document", "document_id": shared}, {"kind": "url", "url": "https://example.com/handbook"}],
    )
    assert ok.status_code == 201, ok.text
    sources = ok.json()["sources"]
    assert sources[0] == {"kind": "document", "document_id": shared, "filename": "handbook.pdf"}
    assert sources[1] == {"kind": "url", "url": "https://example.com/handbook"}
    assert ok.json()["scope_group_id"] == group["id"]


def test_claimant_identity_and_statement_are_immutable_in_postgresql():
    login_id = user_id(USER_EMAIL)
    other = user_id(USER2_EMAIL)
    claim_id = uuid.uuid4()
    run_db(lambda c: c.execute(text(CLAIM), {"id": claim_id, "claimant": login_id, "cat": "policy"}))

    _rejects(
        "UPDATE knowledge_claims SET claimant_user_id = :u WHERE id = :id",
        {"u": other, "id": claim_id},
        "immutable",
    )
    _rejects(
        "UPDATE knowledge_claims SET statement = 'forged rewrite' WHERE id = :id",
        {"id": claim_id},
        "immutable",
    )
    _rejects(
        "UPDATE knowledge_claims SET category = 'legal' WHERE id = :id",
        {"id": claim_id},
        "immutable",
    )
    _rejects(
        "INSERT INTO knowledge_claims (id, claimant_user_id, kind, statement, category, status) "
        "VALUES (:id, :c, 'proposal', 'already withdrawn', 'policy', 'withdrawn')",
        {"id": uuid.uuid4(), "c": login_id},
        "created as submitted",
    )
    _rejects("DELETE FROM knowledge_claims WHERE id = :id", {"id": claim_id}, "never deleted")
    _rejects("UPDATE knowledge_claim_events SET action = 'withdrawn'", {}, "append-only")
    _rejects("DELETE FROM knowledge_claim_events", {}, "append-only")

    run_db(
        lambda c: c.execute(
            text("UPDATE knowledge_claims SET status = 'withdrawn', withdrawn_at = now() WHERE id = :id"),
            {"id": claim_id},
        )
    )
    _rejects(
        "UPDATE knowledge_claims SET status = 'submitted', withdrawn_at = NULL WHERE id = :id",
        {"id": claim_id},
        "can only be withdrawn",
    )
    events = run_db(
        lambda c: c.execute(
            text("SELECT action, from_status, to_status FROM knowledge_claim_events WHERE claim_id = :id ORDER BY id"),
            {"id": claim_id},
        )
    ).all()
    assert [(e[0], e[1], e[2]) for e in events] == [("submitted", None, "submitted"), ("withdrawn", "submitted", "withdrawn")]


def test_forged_approval_author_and_missing_or_withdrawn_claims_are_rejected(client):
    admin, user, user2 = user_id(ADMIN_EMAIL), user_id(USER_EMAIL), user_id(USER2_EMAIL)
    review = uuid.uuid4()
    run_db(lambda c: c.execute(text(GRANT), {"id": review, "user": user, "perm": "knowledge.review", "cat": None, "by": admin}))

    claim_id = uuid.uuid4()
    run_db(lambda c: c.execute(text(CLAIM), {"id": claim_id, "claimant": user2, "cat": "policy"}))
    base = {
        "subject": claim_id, "author": user2, "approver": user, "stage": "review",
        "perm": "knowledge.review", "cat": "policy", "grant": review,
    }
    _rejects(APPROVAL, base | {"author": user}, "fk_governance_approvals_claim_identity|matching knowledge claim")
    _rejects(APPROVAL, base | {"subject": uuid.uuid4()}, "fk_governance_approvals_claim_identity|matching knowledge claim")

    run_db(
        lambda c: c.execute(
            text("UPDATE knowledge_claims SET status = 'withdrawn', withdrawn_at = now() WHERE id = :id"),
            {"id": claim_id},
        )
    )
    _rejects(APPROVAL, base, "submitted claim")

    login(client, USER2_EMAIL)
    live = submit(client, "Late invoices are escalated after 14 days.").json()
    ids = {email: user_id(email) for email in (USER_EMAIL, USER2_EMAIL)}

    def decide(email: str, subject: uuid.UUID):
        async def _run(db):
            approver = await db.get(User, ids[email])
            return await access_control.record_approval(
                db, approver, CTX, subject_type="knowledge_claim", subject_id=subject,
                stage="review", category="policy", scope_group_id=None, rationale="Checked the billing memo.",
            )

        return client.portal.call(in_app(_run))

    with pytest.raises(PermissionDeniedError):
        decide(USER2_EMAIL, uuid.UUID(live["id"]))  # author
    approval = decide(USER_EMAIL, uuid.UUID(live["id"]))
    assert approval.subject_author_user_id == user2  # author comes from the claim, not the caller
    with pytest.raises(NotFoundError):
        decide(USER_EMAIL, uuid.uuid4())
    login(client, USER2_EMAIL)
    other = submit(client, "The finance close is on the fifth business day.").json()
    client.post(f"/api/claims/{other['id']}/withdraw")
    with pytest.raises(ConflictError):
        decide(USER_EMAIL, uuid.UUID(other["id"]))


def test_claims_never_enter_rag_memory_or_chat_answers(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    chunks_before = run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks")))
    memories_before = run_db(lambda c: c.scalar(text("SELECT count(*) FROM memory_items")))
    created = submit(client, "The secret launch budget is 9.1 million.")
    assert created.status_code == 201
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks"))) == chunks_before
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM memory_items"))) == memories_before
    assert app.state.rag.vector_store.ids() == set()

    upload(client, "leave.pdf", make_pdf("Employees receive 20 days of annual leave."))
    process_all(client)
    fake_provider.rag_reply = lambda message: "Employees receive 20 days of annual leave [S1]."
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    answer = client.post(
        f"/api/conversations/{chat_id}/messages", json={"question": "How many days of annual leave do employees receive?"}
    )
    assert answer.status_code == 200
    prompt = "\n".join(message for _, message in fake_provider.calls)
    assert "9.1 million" not in prompt and "secret launch budget" not in prompt
    assert [c["filename"] for c in answer.json()["assistant_message"]["response"]["citations"]] == ["leave.pdf"]

    fake_provider.calls.clear()
    spoken = client.post(
        f"/api/conversations/{chat_id}/messages",
        json={"question": "The refund window is actually 45 days."},
    )
    assert spoken.status_code == 200
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM knowledge_claims"))) == 1  # chat does not capture claims
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM memory_items"))) == memories_before
