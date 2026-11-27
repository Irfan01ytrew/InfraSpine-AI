"""Approval delegation while an approver is away, and reminders for pending approval steps."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.main import app
from app.services.approval_reminders import process_approval_reminders
from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_approval_chains import _chain, _decide, _setup, _submit
from tests.test_enterprise_people import _invite_manager

# Delegation days are calendar days in the organisation's time zone (default Asia/Karachi, see app.platform.timezone).
TODAY = datetime.now(ZoneInfo("Asia/Karachi")).date()


def _delegate(client, delegate_id, **extra):
    return client.post("/api/org/approval-delegations", json={
        "delegate_user_id": delegate_id, "starts_on": str(TODAY), "ends_on": str(TODAY + timedelta(days=5)), "reason": "Leave", **extra,
    })


def _deputy(client, finance):
    """A second Finance manager (no approval limit of their own) to act as USER2's deputy."""
    login(client, ADMIN_EMAIL)
    invitation = _invite_manager(client, finance["id"])
    assert client.post("/api/onboarding/accept", json={"token": invitation["token"], "password": "manager-password-123"}, headers=CSRF).status_code == 200
    return next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == "manager@example.com")


def test_a_delegate_decides_with_the_approvers_authority_until_the_delegation_ends(client):
    finance, ids, _ = _setup(client)
    deputy = _deputy(client, finance)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    login(client, USER_EMAIL)
    before = _submit(client, finance, "20000")
    login(client, "manager@example.com", "manager-password-123")
    assert _decide(client, before["id"]).status_code == 403  # not delegated yet

    login(client, USER2_EMAIL)
    created = _delegate(client, deputy)
    assert created.status_code == 201, created.text
    delegation = created.json()["delegations"][0]
    assert (delegation["delegator_email"], delegation["delegate_email"], delegation["current"]) == (USER2_EMAIL, "manager@example.com", True)
    assert _delegate(client, deputy).status_code == 422  # overlapping dates
    assert _delegate(client, ids[USER2_EMAIL]).status_code == 422  # not to yourself

    login(client, USER_EMAIL)
    small, large = _submit(client, finance, "30000"), _submit(client, finance, "70000")
    login(client, "manager@example.com", "manager-password-123")
    kinds = {(n["request_id"], n["notification_type"]) for n in client.get("/api/service-requests/notifications").json()["items"]}
    assert (small["id"], "approval_step_pending") in kinds  # the deputy is told about new steps
    done = _decide(client, small["id"]).json()
    assert done["status"] == "resolved"
    assert (done["approval_steps"][0]["decided_by_email"], done["approval_steps"][0]["on_behalf_of_email"]) == ("manager@example.com", USER2_EMAIL)
    refused = _decide(client, large["id"])  # USER2's authority is PKR 50,000
    assert refused.status_code == 403 and "authority" in refused.json()["error"]["message"]

    login(client, ADMIN_EMAIL)
    entries = client.get("/api/admin/audit-logs?limit=40").json()["entries"]
    assert next(e for e in entries if e["action"] == "service_request.step_approved")["details"]["on_behalf_of"] == USER2_EMAIL
    assert next(e for e in entries if e["action"] == "service_request.approved")["details"]["limit"] == "50000.00"

    login(client, USER2_EMAIL)
    assert client.post(f"/api/org/approval-delegations/{delegation['id']}/end").json()["delegations"] == []
    login(client, "manager@example.com", "manager-password-123")
    assert _decide(client, before["id"]).status_code == 403


def test_only_administrators_delegate_for_someone_else(client):
    finance, ids, _ = _setup(client)
    login(client, USER_EMAIL)
    assert _delegate(client, ids[ADMIN_EMAIL], delegator_user_id=ids[USER2_EMAIL]).status_code == 403
    assert client.get("/api/org/approval-delegations").json()["delegations"] == []
    login(client, ADMIN_EMAIL)
    made = _delegate(client, ids[USER_EMAIL], delegator_user_id=ids[USER2_EMAIL])
    assert made.status_code == 201 and made.json()["delegations"][0]["delegator_email"] == USER2_EMAIL
    login(client, USER_EMAIL)  # the delegate sees it too
    assert [d["delegator_email"] for d in client.get("/api/org/approval-delegations").json()["delegations"]] == [USER2_EMAIL]
    # The requester cannot approve their own request even as a delegate.
    login(client, ADMIN_EMAIL)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    login(client, USER_EMAIL)
    own = _submit(client, finance, "10000")
    assert _decide(client, own["id"]).status_code == 403


def test_pending_steps_are_reminded_at_most_the_configured_number_of_times(client):
    finance, ids, _ = _setup(client)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    login(client, USER_EMAIL)
    request = _submit(client, finance, "30000")
    now = datetime.now(UTC)

    async def remind(at):
        return await process_approval_reminders(app.state.sessionmaker, now=at, after=timedelta(hours=24), max_reminders=2)

    assert client.portal.call(remind, now) == 0  # not waiting long enough yet
    run_db(lambda conn: conn.execute(text("UPDATE request_approval_steps SET pending_since = :t WHERE request_id = :id"),
                                     {"t": now - timedelta(hours=25), "id": uuid.UUID(request["id"])}))
    assert client.portal.call(remind, now) == 1
    assert client.portal.call(remind, now) == 0  # already reminded at this time
    assert client.portal.call(remind, now + timedelta(hours=25)) == 1
    assert client.portal.call(remind, now + timedelta(hours=40)) == 0  # not yet due after the last reminder
    assert client.portal.call(remind, now + timedelta(hours=50)) == 1  # escalated after two reminders
    assert client.portal.call(remind, now + timedelta(hours=100)) == 0  # escalated once only

    login(client, USER2_EMAIL)
    reminders = [n for n in client.get("/api/service-requests/notifications").json()["items"] if n["notification_type"] == "approval_step_reminder"]
    assert len(reminders) == 1 and reminders[0]["read_at"] is None
    item = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == request["id"])
    step = item["approval_steps"][0]
    assert step["reminder_count"] == 2 and step["pending_since"] and step["escalated_at"]
    assert sum("Reminder" in (e["comment"] or "") for e in item["events"]) == 2
    assert sum((e["comment"] or "").startswith("Escalated") for e in item["events"]) == 1
    assert not [n for n in client.get("/api/service-requests/notifications").json()["items"] if n["notification_type"] == "approval_step_escalated"]
    login(client, ADMIN_EMAIL)  # USER2 reports to the admin, who gets the escalation
    assert [n["request_id"] for n in client.get("/api/service-requests/notifications").json()["items"] if n["notification_type"] == "approval_step_escalated"] == [request["id"]]
    login(client, USER_EMAIL)  # the requester is not sent approval reminders
    assert not [n for n in client.get("/api/service-requests/notifications").json()["items"] if n["notification_type"].startswith("approval_step")]


def test_escalation_goes_to_the_approvers_manager_who_can_then_follow_the_request(client):
    finance, ids, _ = _setup(client)
    login(client, ADMIN_EMAIL)
    other = client.post("/api/admin/access/groups", json={"name": "Operations", "kind": "department"}).json()
    invitation = _invite_manager(client, other["id"])  # a manager outside Finance
    assert client.post("/api/onboarding/accept", json={"token": invitation["token"], "password": "manager-password-123"}, headers=CSRF).status_code == 200
    boss = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == "manager@example.com")
    client.put(f"/api/admin/users/{ids[USER2_EMAIL]}/org", json={"reports_to_id": boss})
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    login(client, USER_EMAIL)
    request = _submit(client, finance, "30000")
    login(client, "manager@example.com", "manager-password-123")
    assert client.get(f"/api/service-requests/{request['id']}").status_code == 404

    now = datetime.now(UTC)
    run_db(lambda conn: conn.execute(text("UPDATE request_approval_steps SET pending_since = :t WHERE request_id = :id"),
                                     {"t": now - timedelta(hours=25), "id": uuid.UUID(request["id"])}))

    async def remind():
        return await process_approval_reminders(app.state.sessionmaker, now=now, after=timedelta(hours=24), max_reminders=0)

    assert client.portal.call(remind) == 1
    assert [n["notification_type"] for n in client.get("/api/service-requests/notifications").json()["items"]] == ["approval_step_escalated"]
    followed = client.get(f"/api/service-requests/{request['id']}")
    assert followed.status_code == 200 and followed.json()["can_decide_step"] is False


def test_approval_emails_are_queued_sent_retried_and_cancelled(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import approval_delegation, approval_reminders

    settings = get_settings().model_copy(update={"approval_email_enabled": True})
    monkeypatch.setattr(approval_delegation, "get_settings", lambda: settings)
    sent, fail = [], {"next": True}

    def fake_send(_settings, address, email_id, kind, reference):
        if fail["next"]:
            fail["next"] = False
            raise ConnectionError("synthetic outage")
        sent.append((address, kind, reference))

    monkeypatch.setattr(approval_reminders, "send_approval_email", fake_send)
    finance, ids, _ = _setup(client)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    login(client, USER_EMAIL)
    first, second = _submit(client, finance, "30000"), _submit(client, finance, "40000")
    now = datetime.now(UTC)

    async def deliver(at):
        return await approval_reminders.deliver_approval_emails(app.state.sessionmaker, settings=settings, now=at)

    assert client.portal.call(deliver, now) == 1  # the first attempt failed and will be retried
    rows = run_db(lambda conn: conn.execute(text("SELECT kind, status FROM approval_emails ORDER BY created_at")))
    assert sorted(tuple(r) for r in rows.all()) == [("pending", "retry"), ("pending", "sent")]
    login(client, USER2_EMAIL)
    _decide(client, second["id"], "reject", "No")
    _decide(client, first["id"], "reject", "No")
    assert client.portal.call(deliver, now + timedelta(hours=1)) == 0  # the retry is cancelled: request closed
    statuses = run_db(lambda conn: conn.execute(text("SELECT status FROM approval_emails")))
    assert sorted(r[0] for r in statuses.all()) == ["cancelled", "sent"]
    assert [(a, k) for a, k, _ in sent] == [(USER2_EMAIL, "pending")] and len(sent[0][2]) == 8
