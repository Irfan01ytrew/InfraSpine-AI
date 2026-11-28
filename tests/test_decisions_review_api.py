"""Step M slice 5: review API — confirm / expire / supersede / revoke,
due list, digest, and the decision.expire approval flow.

Runs on the dedicated `infraspine_decision_test` database only (see
`.test-env-decision125.sh`, never committed). The admin holds
`decision.record` / `decision.read` / `decision.approve` on the org
scope; the plain user holds `decision.read` only; the approver holds
`decision.approve` only. The legacy admin role deliberately implies no
decision permission.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.decision_memory.service import PERMISSION_APPROVE, PERMISSION_READ, PERMISSION_RECORD
from app.main import app
from app.platform import access as platform_access
from app.platform.approvals import decide_approval
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")


async def _tx(fn, *, commit=True):
    """Run `await fn(db)` in one session; commit unless it raises."""
    async with app.state.sessionmaker() as db:
        try:
            result = await fn(db)
        except Exception:
            await db.rollback()
            raise
        if commit:
            await db.commit()
        return result


async def _principal(db, user_id):
    from app.db.models import User

    return platform_access.principal_for(await db.get(User, user_id))


def _session(client, email):
    client.cookies.clear()
    from tests.conftest import login

    login(client, email)
    return dict(client.cookies)


def _as(client, cookies):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


@pytest.fixture
def org_setup(client):
    """Grant the decision roles; return org + dept + user ids."""
    async def setup(db):
        from app.db.models import User
        from sqlalchemy import select

        admin_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        user_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
        user2_id = await db.scalar(select(User.id).where(User.email == USER2_EMAIL))
        admin = await db.get(User, admin_id)
        actor = platform_access.principal_for(admin)
        await platform_access.ensure_role(
            db, "m-test-decisions", {"decision.record", "decision.read", "decision.approve"},
            name="M test decisions")
        await platform_access.ensure_role(
            db, "m-test-reader", {"decision.read"}, name="M test reader")
        await platform_access.ensure_role(
            db, "m-test-approver", {"decision.approve"}, name="M test approver")
        org = await platform_access.ensure_org_scope(db)
        await platform_access.grant_membership(
            db, actor, user_id=admin_id, scope_id=org.id, role_key="m-test-decisions")
        await platform_access.grant_membership(
            db, actor, user_id=user_id, scope_id=org.id, role_key="m-test-reader")
        await platform_access.grant_membership(
            db, actor, user_id=user2_id, scope_id=org.id, role_key="m-test-approver")
        dept = await platform_access.create_scope(
            db, kind="department", parent_id=org.id, name="Dept X")

        from app.api.routes.decisions import ensure_decision_roles

        await ensure_decision_roles(db)
        return {"org_id": org.id, "dept_id": dept.id,
                "admin_id": admin_id, "user_id": user_id, "user2_id": user2_id}

    return client.portal.call(lambda: _tx(setup))


def _body(**overrides):
    payload = {
        "title": "Switch the grain supplier",
        "rationale": "The current supplier missed two deliveries in a row.",
        "kind": "operations",
    }
    payload.update(overrides)
    return payload


def _record(client, cookies, org_id, **overrides):
    response = _as(client, cookies).post(f"/api/decisions/scopes/{org_id}", json=_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _approve_request(client, setup, *, requester_cookies, approver_id, action_type, decision_id, kind,
                     superseded_id=None):
    """Request an approval through the API, approve it as another user; return (id, hash)."""
    payload = {"action_type": action_type, "decision_id": str(decision_id), "kind": kind}
    if superseded_id is not None:
        payload["superseded_id"] = str(superseded_id)
    response = _as(client, requester_cookies).post(
        f"/api/decisions/scopes/{setup['org_id']}/approvals", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    approval_id, action_hash = uuid.UUID(data["approval_id"]), data["action_hash"]

    async def decide(db):
        await decide_approval(db, await _principal(db, approver_id), approval_id,
                              "approve", presented_hash=action_hash)

    client.portal.call(lambda: _tx(decide))
    return approval_id, action_hash


def test_review_confirm_due_and_digest(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    today = datetime.now(UTC).date()

    overdue = _record(client, admin, org_id, title="Overdue",
                      decided_at="2026-11-08T09:00:00+00:00", review_at="2026-11-18")
    _record(client, admin, org_id, title="Due soon",
            decided_at="2026-11-20T09:00:00+00:00", review_at="2026-12-01")
    _record(client, admin, org_id, title="Later",
            decided_at="2026-11-20T09:00:00+00:00", review_at="2027-05-27")

    due = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/reviews/due")
    assert due.status_code == 200, due.text
    assert [row["title"] for row in due.json()["due"]] == ["Overdue", "Due soon"]

    digest = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/reviews/digest")
    assert digest.status_code == 200, digest.text
    body = digest.json()
    assert body["overdue_count"] == 1 and body["due_soon_count"] == 1
    assert body["total_active"] == 3 and body["needs_review_count"] == 0

    reviewed = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{overdue['id']}/review",
        json={"outcome": "confirm", "reason": "Still the right supplier; prices held."})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "active"
    assert reviewed.json()["review_at"] == (today + timedelta(days=180)).isoformat()

    due_after = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/reviews/due")
    assert [row["title"] for row in due_after.json()["due"]] == ["Due soon"]


def test_review_expire_sensitive_needs_approval(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    org_id = org_setup["org_id"]
    decision_id = str(uuid.uuid4())
    record_approval, _ = _approve_request(
        client, org_setup, requester_cookies=admin,
        approver_id=org_setup["user2_id"], action_type="decision.record",
        decision_id=decision_id, kind="finance")
    recorded = _record(client, admin, org_id, title="Close the budget line",
                       kind="finance", decision_id=decision_id,
                       approval_id=str(record_approval))
    assert recorded["id"] == decision_id

    no_approval = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{decision_id}/review",
        json={"outcome": "expire", "reason": "Budget line closed."})
    assert no_approval.status_code == 403, no_approval.text

    expire_approval, _ = _approve_request(
        client, org_setup, requester_cookies=admin,
        approver_id=org_setup["user2_id"], action_type="decision.expire",
        decision_id=decision_id, kind="finance")
    expired = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{decision_id}/review",
        json={"outcome": "expire", "reason": "Budget line closed.",
              "approval_id": str(expire_approval)})
    assert expired.status_code == 200, expired.text
    assert expired.json()["status"] == "expired"


def test_review_supersede_and_revoke(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    org_id = org_setup["org_id"]
    first = _record(client, admin, org_id, title="Use supplier A")
    superseded = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{first['id']}/review",
        json={"outcome": "supersede", "reason": "Supplier A raised prices.",
              "title": "Use supplier B",
              "rationale": "Supplier B quoted 12% less per tonne."})
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["title"] == "Use supplier B"
    assert superseded.json()["supersedes"] == [first["id"]]

    second = _record(client, admin, org_id, title="Temporary measure")
    revoked = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{second['id']}/review",
        json={"outcome": "revoke", "reason": "The board overturned this."})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"


def test_scopes_lists_only_readable_scopes(client, org_setup):
    """Step M slice 6: the UI's scope picker. Org membership covers the
    child dept scope (chain inheritance), so both are listed; a user
    without decision.read sees an empty list, never a 404."""
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    approver = _session(client, USER2_EMAIL)
    org_id, dept_id = str(org_setup["org_id"]), str(org_setup["dept_id"])

    listed = _as(client, admin).get("/api/decisions/scopes")
    assert listed.status_code == 200, listed.text
    scopes = listed.json()["scopes"]
    assert [item["id"] for item in scopes] == [org_id, dept_id]
    assert scopes[0]["kind"] == "org" and scopes[1]["kind"] == "department"

    listed_reader = _as(client, reader).get("/api/decisions/scopes")
    assert [item["id"] for item in listed_reader.json()["scopes"]] == [org_id, dept_id]

    listed_approver = _as(client, approver).get("/api/decisions/scopes")
    assert listed_approver.status_code == 200, listed_approver.text
    assert listed_approver.json()["scopes"] == []

    client.cookies.clear()
    assert client.get("/api/decisions/scopes").status_code == 401


def test_review_authz_and_validation(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    dept_id = org_setup["dept_id"]
    recorded = _record(client, admin, org_id)

    # Unauthenticated.
    client.cookies.clear()
    assert client.post(
        f"/api/decisions/scopes/{org_id}/{recorded['id']}/review",
        json={"outcome": "confirm", "reason": "ok"}).status_code == 401
    # A reader (no decision.record) is 403.
    assert _as(client, reader).post(
        f"/api/decisions/scopes/{org_id}/{recorded['id']}/review",
        json={"outcome": "confirm", "reason": "ok"}).status_code == 403
    # The dept scope is a child of the org, so org membership covers it
    # (platform chain inheritance): an empty due list, not a 404.
    due_dept = _as(client, admin).get(f"/api/decisions/scopes/{dept_id}/reviews/due")
    assert due_dept.status_code == 200, due_dept.text
    assert due_dept.json()["due"] == []
    # An unknown scope id (no scope at all) is 404 for reads and reviews.
    unknown = str(uuid.uuid4())
    assert _as(client, admin).get(
        f"/api/decisions/scopes/{unknown}/reviews/due").status_code == 404
    assert _as(client, admin).post(
        f"/api/decisions/scopes/{unknown}/{recorded['id']}/review",
        json={"outcome": "confirm", "reason": "ok"}).status_code == 404
    # Validation: unknown outcome and empty reason are 422.
    assert _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{recorded['id']}/review",
        json={"outcome": "shelve", "reason": "ok"}).status_code == 422
    assert _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{recorded['id']}/review",
        json={"outcome": "confirm", "reason": ""}).status_code == 422
    # A superseding review without the new decision's fields is 422 from
    # the service (missing title/rationale).
    assert _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{recorded['id']}/review",
        json={"outcome": "supersede", "reason": "new one"}).status_code == 422
