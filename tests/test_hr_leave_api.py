"""Step AG slices 2-3: leave requests on the unified engine.

A department employee requests leave (`hr.leave.request`); the department
manager approves with an approval bound to the exact request
(`hr.leave.approve`); the balance is applied through `execute_once` with
read-back. Nobody approves their own leave; a paused run blocks execution.
Runs against real PostgreSQL via the standard conftest fixtures.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import text

from app.db.models import ROLE_MANAGER, User
from app.hr.service import HrService
from app.main import app
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, _PASSWORD_HASH, login, run_db
from tests.test_finance_invoice_flow import pump


def _user_id(email: str) -> str:
    return str(run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": email})))


def _create_user(email: str, role: str) -> str:
    return str(run_db(lambda conn: conn.scalar(
        User.__table__.insert().values(email=email, role=role, password_hash=_PASSWORD_HASH).returning(User.id)
    )))


def _department(client, name: str) -> str:
    response = client.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_member(department_id: str, user_id: str) -> None:
    run_db(lambda conn: conn.execute(
        text("INSERT INTO access_group_members (group_id, user_id) VALUES (:g, :u)"),
        {"g": department_id, "u": user_id},
    ))


def _add_manager(department_id: str, user_id: str) -> None:
    run_db(lambda conn: conn.execute(
        text("INSERT INTO department_managers (group_id, manager_user_id) VALUES (:g, :u)"),
        {"g": department_id, "u": user_id},
    ))


def _grant_role(client, department_id: str, user_id: str, role: str) -> None:
    # Resolve the admin id OUTSIDE the portal coroutine: run_db() is asyncio.run(),
    # which cannot run inside the portal's already-running event loop.
    admin_id = uuid.UUID(_user_id(ADMIN_EMAIL))

    async def grant(db):
        admin = await db.get(User, admin_id)
        await HrService(db, admin).grant_role(uuid.UUID(department_id), user_id=uuid.UUID(user_id), role=role)
        await db.commit()

    def in_app(fn):
        async def _run():
            async with app.state.sessionmaker() as session:
                return await fn(session)

        return _run

    client.portal.call(in_app(grant))


def _setup(client):
    """Department with employee (hr.employee), manager (hr.manager) and a second user without HR roles."""
    login(client, ADMIN_EMAIL)
    department_id = _department(client, f"HR {uuid.uuid4().hex[:6]}")
    manager_email = f"hr_manager_{uuid.uuid4().hex[:8]}@example.com"
    manager_id = _create_user(manager_email, ROLE_MANAGER)
    _add_manager(department_id, manager_id)
    _add_member(department_id, manager_id)
    _add_member(department_id, _user_id(USER_EMAIL))
    _grant_role(client, department_id, _user_id(USER_EMAIL), "hr.employee")
    _grant_role(client, department_id, manager_id, "hr.manager")
    return {"department_id": department_id, "manager_email": manager_email, "manager_id": manager_id}


def _next_weekday(weekday: int, weeks_ahead: int = 0) -> date:
    """Next date with the given Python weekday (Monday=0), at least tomorrow."""
    day = date.today() + timedelta(days=1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(weeks=weeks_ahead)


def _request(client, department_id, *, leave_type="annual", start=None, end=None, half_day=False, email=USER_EMAIL):
    login(client, email)
    start = start or _next_weekday(0)
    end = end or (start + timedelta(days=2))  # Mon-Wed: 3 business days
    response = client.post("/api/hr/leave", json={
        "group_id": department_id, "leave_type": leave_type,
        "start_date": start.isoformat(), "end_date": end.isoformat(), "half_day": half_day})
    return response


def _approval_hash_db(request_id):
    return run_db(lambda conn: conn.scalar(text(
        "SELECT action_hash FROM platform_approval_requests WHERE subject = :s"), {"s": f"leave_request:{request_id}"}))


def _approval_hash(client, manager_email, request_id):
    login(client, manager_email)
    rows = client.get("/api/approvals").json()["approvals"]
    row = next(r for r in rows if r["subject"] == f"leave_request:{request_id}")
    return row["action_hash"], row["id"]


def _decide(client, ctx, request_id, decision="approve"):
    presented_hash, _ = _approval_hash(client, ctx["manager_email"], request_id)
    login(client, ctx["manager_email"])
    return client.post(f"/api/hr/leave/{request_id}/decide?group_id={ctx['department_id']}",
                       json={"decision": decision, "presented_hash": presented_hash})


def _approve_and_execute(client, ctx, request_id):
    response = _decide(client, ctx, request_id)
    assert response.status_code == 200, response.text
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def _request_row(request_id):
    return run_db(lambda conn: conn.execute(text(
        "SELECT status, days, applied_at IS NOT NULL, approval_id FROM hr_leave_requests WHERE id = :id"),
        {"id": request_id})).one()


def _balance_row(department_id, employee_id, leave_type, year):
    return run_db(lambda conn: conn.execute(text(
        "SELECT entitled, carried, used, pending FROM hr_leave_balances "
        "WHERE group_id = :g AND employee_id = :u AND leave_type = :t AND year = :y"),
        {"g": department_id, "u": employee_id, "t": leave_type, "y": year})).one_or_none()


def _wf_run(request_id):
    return run_db(lambda conn: conn.execute(text(
        "SELECT id, status, phase FROM wf_runs WHERE origin = 'hr' AND origin_ref = :id"), {"id": request_id})).all()


def _wf_steps(run_id):
    return run_db(lambda conn: conn.execute(text(
        "SELECT node_id, status, verified_at IS NOT NULL FROM wf_steps WHERE run_id = :id ORDER BY node_id"),
        {"id": run_id})).all()


def _verified_executions(request_id):
    return run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM platform_action_executions WHERE subject = :s AND status = 'verified'"),
        {"s": f"leave_request:{request_id}"}))


# --------------------------------------------------------------------------- request validation

def test_request_creates_pending_leave_with_approval_and_run(client):
    ctx = _setup(client)
    response = _request(client, ctx["department_id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending" and body["days"] == "3" and body["approval_id"]
    assert body["workflow_run_id"]
    assert len(_wf_run(body["id"])) == 1
    assert _wf_run(body["id"])[0][1] in {"queued", "running"}


def test_request_rejected_when_backdated(client):
    ctx = _setup(client)
    response = _request(client, ctx["department_id"], start=date.today() - timedelta(days=1),
                        end=date.today() + timedelta(days=1))
    assert response.status_code == 422, response.text
    assert "backdated" in response.text


def test_request_rejected_when_overlapping(client):
    ctx = _setup(client)
    start = _next_weekday(0)
    assert _request(client, ctx["department_id"], start=start, end=start + timedelta(days=2)).status_code == 201
    response = _request(client, ctx["department_id"], start=start + timedelta(days=2),
                        end=start + timedelta(days=4))
    assert response.status_code == 422, response.text
    assert "overlaps_existing" in response.text


def test_request_rejected_when_exceeds_max_consecutive_days(client):
    ctx = _setup(client)
    start = _next_weekday(0)
    response = _request(client, ctx["department_id"], start=start, end=start + timedelta(days=14))  # 11 business days
    assert response.status_code == 422, response.text
    assert "exceeds_max_consecutive_days" in response.text


def test_request_rejected_when_insufficient_balance(client):
    ctx = _setup(client)
    start = _next_weekday(0)
    # Sick entitlement is 10: take all of it, then one more day must fail.
    first = _request(client, ctx["department_id"], leave_type="sick", start=start,
                     end=start + timedelta(days=11))
    assert first.status_code == 201, first.text
    _approve_and_execute(client, ctx, first.json()["id"])
    response = _request(client, ctx["department_id"], leave_type="sick",
                        start=_next_weekday(0, weeks_ahead=3), end=_next_weekday(0, weeks_ahead=3))
    assert response.status_code == 422, response.text
    assert "insufficient_balance" in response.text


def test_half_day_request_counts_half_a_day(client):
    ctx = _setup(client)
    day = _next_weekday(0)
    response = _request(client, ctx["department_id"], start=day, end=day, half_day=True)
    assert response.status_code == 201, response.text
    assert response.json()["days"] == "0.5"


# --------------------------------------------------------------------------- authorization

def test_non_member_gets_404(client):
    ctx = _setup(client)
    login(client, USER2_EMAIL)  # seeded user, no membership in this department
    response = client.post("/api/hr/leave", json={
        "group_id": ctx["department_id"], "leave_type": "annual",
        "start_date": _next_weekday(0).isoformat(), "end_date": _next_weekday(0).isoformat()})
    assert response.status_code == 404, response.text


def test_member_without_permission_gets_403(client):
    ctx = _setup(client)
    _grant_role(client, ctx["department_id"], _user_id(USER2_EMAIL), "hr.employee")
    login(client, USER2_EMAIL)
    # hr.employee can list their own leave (they hold hr.leave.request)...
    assert client.get(f"/api/hr/leave?group_id={ctx['department_id']}").status_code == 200
    # ...but not the team's leave (that needs hr.leave.approve).
    response = client.get(f"/api/hr/leave/team?group_id={ctx['department_id']}")
    assert response.status_code == 403, response.text


def test_other_department_manager_cannot_decide(client):
    ctx = _setup(client)
    other = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    login(client, other["manager_email"])
    # The other manager cannot even see the approval: the inbox is scoped to their departments.
    rows = client.get("/api/approvals").json()["approvals"]
    assert not [r for r in rows if r["subject"] == f"leave_request:{request_id}"]
    # Deciding blind (hash from the owning department) is refused as not found.
    presented_hash = _approval_hash_db(request_id)
    response = client.post(f"/api/hr/leave/{request_id}/decide?group_id={ctx['department_id']}",
                           json={"decision": "approve", "presented_hash": presented_hash})
    assert response.status_code == 404, response.text


def test_requester_cannot_approve_own_leave(client):
    ctx = _setup(client)
    # The manager requests their own leave, then tries to approve it.
    response = _request(client, ctx["department_id"], email=ctx["manager_email"])
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    login(client, ctx["manager_email"])
    presented_hash, _ = _approval_hash(client, ctx["manager_email"], request_id)
    response = client.post(f"/api/hr/leave/{request_id}/decide?group_id={ctx['department_id']}",
                           json={"decision": "approve", "presented_hash": presented_hash})
    assert response.status_code == 403, response.text
    assert _request_row(request_id)[0] == "pending"


def test_wrong_hash_decide_rejected(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/decide?group_id={ctx['department_id']}",
                           json={"decision": "approve", "presented_hash": "0" * 64})
    assert response.status_code == 409, response.text
    assert _request_row(request_id)[0] == "pending"


def test_expired_approval_decide_rejected(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    run_db(lambda conn: conn.execute(text(
        "UPDATE platform_approval_requests SET expires_at = now() - interval '1 hour' "
        "WHERE subject = :s"), {"s": f"leave_request:{request_id}"}))
    presented_hash = _approval_hash_db(request_id)
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/decide?group_id={ctx['department_id']}",
                           json={"decision": "approve", "presented_hash": presented_hash})
    assert response.status_code == 409, response.text
    assert _request_row(request_id)[0] == "pending"


# --------------------------------------------------------------------------- approve + execute

def test_manager_approves_and_execute_applies_balance(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    body = _approve_and_execute(client, ctx, request_id)
    assert body["status"] == "approved" and body["applied_at"]
    assert _request_row(request_id)[2] is True  # applied
    balance = _balance_row(ctx["department_id"], _user_id(USER_EMAIL), "annual", _next_weekday(0).year)
    assert balance is not None and float(balance[0]) == 20.0 and float(balance[2]) == 3.0 and float(balance[3]) == 0.0
    assert _verified_executions(request_id) == 1


def test_execute_twice_applies_balance_once(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    _approve_and_execute(client, ctx, request_id)
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 200, response.text
    assert _verified_executions(request_id) == 1
    balance = _balance_row(ctx["department_id"], _user_id(USER_EMAIL), "annual", _next_weekday(0).year)
    assert float(balance[2]) == 3.0  # used days not doubled


def test_reject_flow_blocks_execution(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    response = _decide(client, ctx, request_id, decision="reject")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 409, response.text


def test_cancel_pending_request_cancels_approval(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    login(client, USER_EMAIL)
    response = client.post(f"/api/hr/leave/{request_id}/cancel?group_id={ctx['department_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    approval = run_db(lambda conn: conn.scalar(text(
        "SELECT status FROM platform_approval_requests WHERE subject = :s"), {"s": f"leave_request:{request_id}"}))
    assert approval == "cancelled"
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------- lists and balance

def test_team_and_my_lists_are_scoped(client):
    ctx = _setup(client)
    mine = _request(client, ctx["department_id"]).json()["id"]
    other = _request(client, ctx["department_id"], email=ctx["manager_email"]).json()["id"]
    login(client, USER_EMAIL)
    listed = {row["id"] for row in client.get(f"/api/hr/leave?group_id={ctx['department_id']}").json()["leave"]}
    assert listed == {mine}
    login(client, ctx["manager_email"])
    listed = {row["id"] for row in client.get(f"/api/hr/leave/team?group_id={ctx['department_id']}").json()["leave"]}
    assert listed == {mine, other}


def test_balance_endpoint_reports_remaining(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    year = _next_weekday(0).year
    login(client, USER_EMAIL)
    body = client.get(f"/api/hr/leave/{request_id}/balance?group_id={ctx['department_id']}&year={year}").json()
    assert body["remaining"] == 17.0 and body["pending"] == 3.0  # 20 entitled - 3 pending
    _approve_and_execute(client, ctx, request_id)
    login(client, USER_EMAIL)
    body = client.get(f"/api/hr/leave/{request_id}/balance?group_id={ctx['department_id']}&year={year}").json()
    assert body["remaining"] == 17.0 and body["used"] == 3.0 and body["pending"] == 0.0


# --------------------------------------------------------------------------- unified engine

def test_workflow_run_executes_via_engine(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    pump(client, 6)
    run_id, status, phase = _wf_run(request_id)[0]
    assert status in {"queued", "running"}
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"][0] == "waiting"  # waits for the manager's decision
    assert steps["execute"][0] == "blocked"

    _decide(client, ctx, request_id)
    pump(client, 6)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"] == ("completed", True)
    assert steps["execute"][0] == "action"  # waits for someone holding hr.leave.approve

    # The requester cannot take the execute step.
    login(client, USER_EMAIL)
    detail = client.get(f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    execute_step = next(s for s in detail.json()["steps"] if s["node_id"] == "execute")
    response = client.post(
        f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/steps/{execute_step['id']}/perform")
    assert response.status_code in {403, 404}, response.text

    # The department manager performs it; the engine applies the balance once.
    login(client, ctx["manager_email"])
    response = client.post(
        f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/steps/{execute_step['id']}/perform")
    assert response.status_code == 200, response.text
    pump(client, 6)
    assert _wf_run(request_id)[0][1] == "completed"
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["execute"] == ("completed", True)
    assert _request_row(request_id)[2] is True
    assert _verified_executions(request_id) == 1


def test_pause_blocks_execution_and_resume_continues(client):
    ctx = _setup(client)
    request_id = _request(client, ctx["department_id"]).json()["id"]
    pump(client, 6)
    run_id = _wf_run(request_id)[0][0]
    login(client, ctx["manager_email"])
    assert client.post(f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/control/pause").status_code == 200
    assert _wf_run(request_id)[0][1] == "paused"
    _decide(client, ctx, request_id)
    pump(client, 3)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"][0] == "waiting"  # approval recorded, run does not advance while paused
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 409, response.text
    assert client.post(f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/control/resume").status_code == 200
    pump(client, 6)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"] == ("completed", True)  # last verified step kept, not re-decided
    login(client, ctx["manager_email"])
    response = client.post(f"/api/hr/leave/{request_id}/execute?group_id={ctx['department_id']}")
    assert response.status_code == 200, response.text
    pump(client, 6)
    assert _wf_run(request_id)[0][1] == "completed"
    assert _verified_executions(request_id) == 1
