"""ADR-004 WP1: scoped access (groups, resource ACLs), source-ACL quarantine, revocation on the next
request, withheld saved answers, permission grants and database-enforced separation of duties.
Runs against real PostgreSQL. All documents, people and identifiers are synthetic."""

import uuid

import pytest
from sqlalchemy import text

from app.core.errors import PermissionDeniedError
from app.core.security import SESSION_COOKIE_NAME
from app.db.models import User
from app.main import app
from app.services import access_control
from app.services.audit import AuditContext
from app.services.conversations import WITHHELD_ANSWER
from app.services.source_acl import SourceAce, sync_directory_group, sync_document_acl
from tests.conftest import _PASSWORD_HASH, ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, ask, make_pdf, process_all, upload

ADMIN2_EMAIL = "admin2@example.com"
PLAN = "Project Atlas plan: the launch codename is BLUEHERON and the approved budget is 4.2 million."
NOTES = "Meeting notes about the launch codename discussion and the venue for the launch party."
SECRET = "BLUEHERON"
CTX = AuditContext(None, "connector@test", None, None)


def cites_plan(message: str) -> str:
    return "The launch codename is BLUEHERON [S1]." if SECRET in message else "The notes mention a venue [S1]."


def add_admin2() -> None:
    run_db(lambda c: c.execute(User.__table__.insert().values(email=ADMIN2_EMAIL, role="admin", password_hash=_PASSWORD_HASH)))


def user_id(email: str) -> uuid.UUID:
    return run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email}))


def new_group(client, name: str, kind: str = "project", external_id: str | None = None) -> str:
    body = {"name": name, "kind": kind} | ({"external_id": external_id} if external_id else {})
    response = client.post("/api/admin/access/groups", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def set_access(client, doc_id: str, visibility: str, users: list[str] = (), groups: list[str] = ()):
    return client.put(
        f"/api/rag/documents/{doc_id}/access",
        json={"visibility": visibility, "user_emails": list(users), "group_ids": list(groups)},
    )


def admin_plan(client) -> str:
    """As admin (provider active): upload and index the synthetic plan. Leaves the admin signed in."""
    activate_provider(client)
    doc_id = upload(client, "atlas-plan.pdf", make_pdf(PLAN)).json()["id"]
    process_all(client)
    return doc_id


def can_see(client, doc_id: str) -> bool:
    listed = doc_id in {d["id"] for d in client.get("/api/rag/documents").json()["documents"]}
    status = client.get(f"/api/rag/documents/{doc_id}").status_code
    image = client.get(f"/api/rag/documents/{doc_id}/pages/1/image").status_code
    found = SECRET in ask(client, "What is the launch codename?").text
    assert len({listed, status == 200, image == 200, found}) == 1, (listed, status, image, found)  # all paths agree
    return listed


def resume(client, session_token: str) -> None:
    """Continue an earlier session (as its browser would) instead of signing in again."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, session_token)


def in_app(fn):
    """Run `await fn(session)` on the app's event loop with its own database session."""

    async def _run():
        async with app.state.sessionmaker() as session:
            return await fn(session)

    return _run


# --------------------------------------------------------------------------- scoped document access


def test_restricted_document_is_visible_only_to_its_acl_principals(client, fake_provider):
    add_admin2()
    fake_provider.rag_reply = cites_plan
    doc_id = admin_plan(client)
    project = new_group(client, "Atlas")
    assert client.post(f"/api/admin/access/groups/{project}/members", json={"email": USER_EMAIL}).status_code == 204
    restricted = set_access(client, doc_id, "restricted", groups=[project])
    assert restricted.status_code == 200 and restricted.json()["visibility"] == "restricted"

    assert can_see(client, doc_id)  # the uploading admin keeps owner access
    login(client, USER_EMAIL)
    assert can_see(client, doc_id)  # group member
    assert set_access(client, doc_id, "private").status_code == 403  # members cannot change access

    missing = client.get(f"/api/rag/documents/{uuid.uuid4()}")
    fake_provider.calls.clear()
    for email in (USER2_EMAIL, ADMIN2_EMAIL):  # outsiders, administrators included
        login(client, email)
        assert not can_see(client, doc_id)
        denied = client.get(f"/api/rag/documents/{doc_id}")
        assert denied.status_code == 404 and denied.json() == missing.json()  # indistinguishable from "no such id"
    assert all(SECRET not in message for _, message in fake_provider.calls)  # nothing reached the LLM for them

    # Narrowing applies on the very next request: from the group to one named user.
    login(client, ADMIN_EMAIL)
    assert set_access(client, doc_id, "restricted", users=[USER2_EMAIL]).status_code == 200
    login(client, USER_EMAIL)
    assert not can_see(client, doc_id)
    login(client, USER2_EMAIL)
    assert can_see(client, doc_id)

    # Regular users still cannot publish beyond themselves; private/shared keep their meaning.
    own = upload(client, "mine.pdf", make_pdf(NOTES)).json()["id"]
    assert set_access(client, own, "restricted", groups=[project]).status_code == 403
    assert set_access(client, own, "shared").status_code == 403
    login(client, ADMIN_EMAIL)
    assert set_access(client, doc_id, "restricted").status_code == 422  # restricted needs a principal
    assert set_access(client, doc_id, "shared").status_code == 200
    login(client, ADMIN2_EMAIL)
    assert can_see(client, doc_id)


def test_revocation_applies_on_the_next_request_and_withholds_saved_answers(client, fake_provider):
    fake_provider.rag_reply = cites_plan
    doc_id = admin_plan(client)
    project = new_group(client, "Atlas")
    client.post(f"/api/admin/access/groups/{project}/members", json={"email": USER_EMAIL})
    set_access(client, doc_id, "restricted", groups=[project])

    login(client, USER_EMAIL)
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    first = client.post(f"/api/conversations/{chat_id}/messages", json={"question": "What is the launch codename?"})
    assert SECRET in first.json()["assistant_message"]["content"]

    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/admin/access/groups/{project}/members/{user_id(USER_EMAIL)}").status_code == 204

    resume(client, session_token)  # the same session, no re-login
    assert client.get(f"/api/rag/documents/{doc_id}").status_code == 404
    upload(client, "notes.pdf", make_pdf(NOTES))  # a source the user keeps, so the follow-up reaches the LLM
    process_all(client)
    reopened = client.get(f"/api/conversations/{chat_id}")
    assert SECRET not in reopened.text and "atlas-plan.pdf" not in reopened.text
    answer = reopened.json()["messages"][1]
    assert answer["withheld"] is True and answer["content"] == WITHHELD_ANSWER
    assert answer["response"]["answer"] == WITHHELD_ANSWER and answer["response"]["chart"] is None
    assert answer["sources_unavailable"] == 1 and answer["response"]["page_images"] == []
    citation = answer["response"]["citations"][0]
    assert citation["available"] is False and citation["snippet"] == "" and citation["filename"] == "Unavailable document"

    fake_provider.calls.clear()
    follow_up = client.post(f"/api/conversations/{chat_id}/messages", json={"question": "Which launch codename venue?"})
    assert follow_up.status_code == 200 and fake_provider.calls  # the LLM was asked, using the notes only
    assert all(SECRET not in message for _, message in fake_provider.calls)  # neither retrieved nor in history
    assert {c["filename"] for c in follow_up.json()["assistant_message"]["response"]["citations"]} <= {"notes.pdf"}

    # The saved row is intact: restoring access shows the answer again.
    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    assert client.post(f"/api/admin/access/groups/{project}/members", json={"email": USER_EMAIL}).status_code == 204
    resume(client, session_token)
    restored = client.get(f"/api/conversations/{chat_id}").json()["messages"][1]
    assert restored["withheld"] is False and SECRET in restored["content"]


async def _strip_source_tracking(conn):
    """Recreate an answer saved before source tracking. Messages are append-only since migration 0084, so the
    fixture bypasses only that trigger, inside this one transaction, to fabricate the legacy row shape."""
    await conn.execute(text("ALTER TABLE conversation_messages DISABLE TRIGGER trg_conversation_messages_immutable"))
    await conn.execute(text("UPDATE conversation_messages SET payload = payload - 'source_document_ids'"))
    await conn.execute(text("ALTER TABLE conversation_messages ENABLE TRIGGER trg_conversation_messages_immutable"))


def test_saved_answers_from_before_source_tracking_fall_back_to_citations(client, fake_provider):
    fake_provider.rag_reply = cites_plan
    doc_id = admin_plan(client)
    set_access(client, doc_id, "shared")
    login(client, USER_EMAIL)
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    client.post(f"/api/conversations/{chat_id}/messages", json={"question": "What is the launch codename?"})
    run_db(_strip_source_tracking)

    login(client, ADMIN_EMAIL)
    set_access(client, doc_id, "private")
    login(client, USER_EMAIL)
    answer = client.get(f"/api/conversations/{chat_id}").json()["messages"][1]
    assert answer["withheld"] is True and SECRET not in answer["content"]


# --------------------------------------------------------------------------- source ACL quarantine


def test_unknown_or_unsupported_source_acls_quarantine_without_admin_access(client, fake_provider):
    fake_provider.rag_reply = cites_plan
    doc_id = admin_plan(client)
    # Simulate a file-server document: the trigger quarantines it until the first valid ACL sync.
    run_db(lambda c: c.execute(
        text("UPDATE documents SET source_system = 'file_server', visibility = 'restricted' WHERE id = :d"), {"d": doc_id}
    ))
    run_db(lambda c: c.execute(text("UPDATE users SET external_id = 'S-1-5-21-1001' WHERE email = :e"), {"e": USER_EMAIL}))
    client.portal.call(in_app(lambda db: sync_directory_group(db, "S-1-5-21-5000", "Atlas team", "project", ["S-1-5-21-1001"], CTX)))

    def sync(*entries: SourceAce):
        return client.portal.call(in_app(lambda db: sync_document_acl(db, uuid.UUID(doc_id), list(entries), CTX)))

    def readers() -> set[str]:
        out = set()
        for email in (ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL):
            login(client, email)
            if can_see(client, doc_id):
                out.add(email)
        return out

    assert readers() == set()  # not yet synced: nobody, including the admin who owns the row
    login(client, ADMIN_EMAIL)
    queue = client.get("/api/admin/access/quarantine")
    assert [(d["id"], d["reason"]) for d in queue.json()["documents"]] == [(doc_id, "acl_not_synced")]
    assert "atlas-plan" not in queue.text and SECRET not in queue.text  # identifiers and reasons only

    group = SourceAce("group", "S-1-5-21-5000")
    for entries, reason in (
        ([SourceAce("group", "S-1-5-21-9999")], "unresolved_principal"),
        ([], "empty_acl"),
        ([group, SourceAce("user", "S-1-5-21-1002", access="deny")], "unsupported_deny_entry"),
        ([SourceAce("group", "S-1-5-21-5000", rights="take_ownership")], "unsupported_right"),
        ([SourceAce("computer", "S-1-5-21-7")], "unsupported_principal_type"),
        ([SourceAce("group", "S-1-1-0", access="audit")], "unsupported_entry_type"),
    ):
        result = sync(*entries)
        assert (result.acl_status, result.reason) == ("quarantined", reason)
        assert readers() == set()

    assert sync(group).acl_status == "active"
    assert readers() == {USER_EMAIL}  # exactly the source ACL; the owning admin gets nothing implicit
    login(client, ADMIN_EMAIL)
    assert client.get("/api/admin/access/quarantine").json()["documents"] == []

    # A later unsupported ACL closes access again on the next request.
    sync(group, SourceAce("user", "S-1-5-21-1002", access="deny"))
    assert readers() == set()
    sync(group)
    login(client, USER_EMAIL)
    assert set_access(client, doc_id, "shared").status_code == 403  # synced ACLs are never edited or widened here
    login(client, ADMIN_EMAIL)
    assert client.post(f"/api/admin/access/groups/{uuid.uuid4()}/members", json={"email": USER_EMAIL}).status_code == 404
    directory_group = run_db(lambda c: c.scalar(text("SELECT id FROM access_groups WHERE external_id = 'S-1-5-21-5000'")))
    assert client.post(f"/api/admin/access/groups/{directory_group}/members", json={"email": USER2_EMAIL}).status_code == 409


def test_synced_documents_are_inserted_quarantined_whatever_the_writer_sends():
    async def insert(conn):
        owner = await conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": ADMIN_EMAIL})
        return (await conn.execute(text(
            "INSERT INTO documents (id, owner_id, visibility, filename, storage_key, sha256, size_bytes, "
            "source_system, acl_status) VALUES (gen_random_uuid(), :o, 'restricted', 'x.pdf', 'k', :s, 1, "
            "'file_server', 'active') RETURNING acl_status, acl_quarantine_reason"
        ), {"o": owner, "s": "0" * 64})).one()

    assert tuple(run_db(insert)) == ("quarantined", "acl_not_synced")
    with pytest.raises(Exception, match="synced_is_restricted"):
        run_db(lambda c: c.execute(text("UPDATE documents SET visibility = 'shared'")))


# --------------------------------------------------------------------------- grants and separation of duties


def test_permission_grants_are_admin_only_and_never_self_granted(client):
    login(client, ADMIN_EMAIL)
    body = {"email": USER_EMAIL, "permission": "knowledge.review", "category": "policy"}
    grant = client.post("/api/admin/access/grants", json=body)
    assert grant.status_code == 201 and grant.json()["email"] == USER_EMAIL
    assert client.post("/api/admin/access/grants", json=body).status_code == 409  # one active grant per scope
    assert client.post("/api/admin/access/grants", json=body | {"email": ADMIN_EMAIL}).status_code == 403

    revoked = client.post(f"/api/admin/access/grants/{grant.json()['id']}/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert client.post(f"/api/admin/access/grants/{grant.json()['id']}/revoke").status_code == 409
    assert client.get("/api/admin/access/grants").json()["grants"] == []
    assert len(client.get("/api/admin/access/grants?include_revoked=true").json()["grants"]) == 1
    assert client.post("/api/admin/access/grants", json=body).status_code == 201  # re-grant after revocation

    login(client, USER_EMAIL)
    for method, path in (
        ("get", "/groups"),
        ("get", "/grants"),
        ("get", "/permission-catalog"),
        ("get", "/quarantine"),
        ("post", "/grants"),
    ):
        assert getattr(client, method)(f"/api/admin/access{path}", **({"json": body} if method == "post" else {})).status_code == 403
    actions = {r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs"))).all()}
    assert {"access.permission_granted", "access.permission_revoked"} <= actions


def _rejects(sql: str, params: dict, match: str) -> None:
    with pytest.raises(Exception, match=match):
        run_db(lambda c: c.execute(text(sql), params))


GRANT = (
    "INSERT INTO permission_grants (id, user_id, permission, category, granted_by) "
    "VALUES (:id, :user, :perm, :cat, :by) RETURNING id"
)
CLAIM = (
    "INSERT INTO knowledge_claims (id, claimant_user_id, kind, statement, category) "
    "VALUES (:id, :claimant, 'proposal', 'synthetic organizational statement', :cat)"
)
APPROVAL = (
    "INSERT INTO governance_approvals (subject_type, subject_id, subject_author_user_id, approver_user_id, stage, "
    "permission, category, permission_grant_id, rationale) "
    "VALUES ('knowledge_claim', :subject, :author, :approver, :stage, :perm, :cat, :grant, 'checked the source')"
)


def test_separation_of_duties_is_enforced_by_postgresql_even_without_the_service():
    admin, user, user2 = user_id(ADMIN_EMAIL), user_id(USER_EMAIL), user_id(USER2_EMAIL)
    review, publish, user_publish = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def grants(conn):
        for gid, who, perm, cat in (
            (review, user, "knowledge.review", "policy"),
            (publish, user2, "knowledge.publish", None),
            (user_publish, user, "knowledge.publish", None),
        ):
            await conn.execute(text(GRANT), {"id": gid, "user": who, "perm": perm, "cat": cat, "by": admin})

    run_db(grants)
    g = {"id": uuid.uuid4(), "perm": "knowledge.review", "cat": None}
    _rejects(GRANT, g | {"user": admin, "by": admin}, "no_self_grant")  # single-row CHECK
    _rejects(GRANT, g | {"user": user2, "by": user}, "active administrator")  # trigger: grantor must be an admin
    _rejects("UPDATE permission_grants SET category = NULL WHERE id = :g", {"g": review}, "can only be revoked")
    _rejects("DELETE FROM permission_grants WHERE id = :g", {"g": review}, "never deleted")

    subject = uuid.uuid4()
    own_subject = uuid.uuid4()
    run_db(lambda c: c.execute(text(CLAIM), {"id": subject, "claimant": admin, "cat": "policy"}))
    run_db(lambda c: c.execute(text(CLAIM), {"id": own_subject, "claimant": user, "cat": "policy"}))
    base = {"subject": subject, "author": admin, "cat": "policy"}
    review_by = lambda who, grant: base | {"approver": who, "stage": "review", "perm": "knowledge.review", "grant": grant}  # noqa: E731
    publish_by = lambda who, grant: base | {"approver": who, "stage": "publish", "perm": "knowledge.publish", "grant": grant}  # noqa: E731

    _rejects(
        APPROVAL, review_by(user, review) | {"subject": own_subject, "author": user}, "no_self_approval"
    )  # author cannot approve
    _rejects(APPROVAL, review_by(user2, review), "fk_governance_approvals_own_grant")  # someone else's grant
    _rejects(APPROVAL, review_by(user, review) | {"cat": "financial"}, "does not cover this category")
    _rejects(APPROVAL, publish_by(user2, publish), "prior review")  # publishing needs a review first
    run_db(lambda c: c.execute(text(APPROVAL), review_by(user, review)))
    _rejects(APPROVAL, publish_by(user, user_publish), "uq_governance_approvals_one_role")  # same person twice
    run_db(lambda c: c.execute(text(APPROVAL), publish_by(user2, publish)))  # two distinct people: allowed
    _rejects(APPROVAL, review_by(user, review), "uq_governance_approvals_one_stage|one_role")
    _rejects("UPDATE governance_approvals SET rationale = 'edited'", {}, "append-only")
    _rejects("DELETE FROM governance_approvals", {}, "append-only")

    run_db(lambda c: c.execute(
        text("UPDATE permission_grants SET revoked_at = now(), revoked_by = :a WHERE id = :g"), {"a": admin, "g": review}
    ))
    later = uuid.uuid4()
    run_db(lambda c: c.execute(text(CLAIM), {"id": later, "claimant": admin, "cat": "policy"}))
    _rejects(APPROVAL, review_by(user, review) | {"subject": later}, "grant is revoked")
    _rejects("UPDATE permission_grants SET revoked_at = now(), revoked_by = :a WHERE id = :g", {"a": admin, "g": review}, "already revoked")


def test_decisions_recheck_the_grant_at_decision_time(client):
    login(client, USER2_EMAIL)
    first = client.post(
        "/api/claims", json={"kind": "proposal", "statement": "The signed contract uses UK law.", "category": "legal"}
    )
    second = client.post(
        "/api/claims", json={"kind": "proposal", "statement": "Late fees are waived for the first week.", "category": "legal"}
    )
    third = client.post(
        "/api/claims", json={"kind": "correction", "statement": "The notice period is 30 days, not 14.", "category": "legal"}
    )
    assert {first.status_code, second.status_code, third.status_code} == {201}
    login(client, ADMIN_EMAIL)
    grant = client.post("/api/admin/access/grants", json={"email": USER_EMAIL, "permission": "knowledge.review"}).json()
    ids = {email: user_id(email) for email in (USER_EMAIL, USER2_EMAIL)}

    def decide(email: str, subject: uuid.UUID):
        async def _run(db):
            approver = await db.get(User, ids[email])
            return await access_control.record_approval(
                db, approver, CTX, subject_type="knowledge_claim", subject_id=subject,
                stage="review", category="legal", scope_group_id=None, rationale="Matches the signed contract.",
            )
        return client.portal.call(in_app(_run))

    approval = decide(USER_EMAIL, uuid.UUID(first.json()["id"]))
    assert str(approval.permission_grant_id) == grant["id"]  # the decision records the grant it relied on
    with pytest.raises(PermissionDeniedError):
        decide(USER2_EMAIL, uuid.UUID(second.json()["id"]))  # the author, and without any grant
    client.post(f"/api/admin/access/grants/{grant['id']}/revoke")
    with pytest.raises(PermissionDeniedError):
        decide(USER_EMAIL, uuid.UUID(third.json()["id"]))  # revoked a moment ago: rejected on the next decision
