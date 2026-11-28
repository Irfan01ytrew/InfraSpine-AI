"""Step Q (slices 2-3): approval-gated control-room controls + worker health.

Proves, on a throwaway ``*_test`` database only, that:

* pausing/resuming a unified run from the control room needs
  ``workflow.manage`` AND an exact-action approval: the requester
  (a department manager) asks, a different manager approves the exact
  action hash, and the executor consumes the approval once;
* an approval for one action cannot run another action, cannot be
  reused, and cannot be approved by its own requester;
* a plain member gets 403 on every control endpoint;
* pausing a studio-origin run pauses its unified mirror (the Studio
  run itself is left alone — it has no ``paused`` status — and resumes
  with the mirror);
* pausing/resuming/assigning an Agent Studio agent works the same way,
  the Master Agent cannot be reassigned, and an agent can only be
  assigned to a member of the same scope;
* ``GET /health/ready`` reports workflow-engine health (kill switch,
  last recorded engine activity, due runs).
"""

import uuid

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, PASSWORD, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue
from tests.test_workflow_engine import publish, save, setup_scope, trigger

OUTSIDER_EMAIL = "control-room-outsider-2@example.com"


def _make_manager(scope, people):
    """Promote USER2 to department manager of the scope. The
    ``department_managers`` trigger requires the user to hold the
    ``manager`` role, so promote the role first."""
    async def seed(conn):
        await conn.execute(
            text("UPDATE users SET role = 'manager' WHERE id = :u"),
            {"u": people[USER2_EMAIL]},
        )
        await conn.execute(
            text("INSERT INTO department_managers(group_id, manager_user_id)"
                 " VALUES (:g, :u)"),
            {"g": scope, "u": people[USER2_EMAIL]},
        )
    run_db(seed)


def _request_run(client, scope, run_id, command):
    response = client.post(
        f"/api/control-room/scopes/{scope}/runs/{run_id}/control-request",
        json={"command": command},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client, approval):
    response = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "approve", "presented_hash": approval["action_hash"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _execute_run(client, scope, run_id, command, approval_id):
    return client.post(
        f"/api/control-room/scopes/{scope}/runs/{run_id}/control",
        json={"command": command, "approval_id": approval_id},
    )


def _request_agent(client, scope, agent_id, command, assignee_id=None):
    body = {"command": command}
    if assignee_id is not None:
        body["assignee_id"] = assignee_id
    response = client.post(
        f"/api/control-room/scopes/{scope}/agents/{agent_id}/control-request",
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _execute_agent(client, scope, agent_id, command, approval_id, assignee_id=None):
    body = {"command": command, "approval_id": approval_id}
    if assignee_id is not None:
        body["assignee_id"] = assignee_id
    return client.post(
        f"/api/control-room/scopes/{scope}/agents/{agent_id}/control",
        json=body,
    )


def _run_status(run_id):
    async def query(conn):
        return (await conn.execute(
            text("SELECT status FROM wf_runs WHERE id = :id"), {"id": str(run_id)}
        )).scalar_one()
    return run_db(query)


def _mirror_run(studio_run_id):
    """The unified run mirroring one Studio run, or None."""
    async def query(conn):
        row = (await conn.execute(
            text("SELECT id, status FROM wf_runs WHERE origin = 'studio' AND origin_ref = :ref"),
            {"ref": str(studio_run_id)},
        )).first()
        return None if row is None else {"id": str(row[0]), "status": row[1]}
    return run_db(query)


def _mirror_pending(client):
    """The engine tick's pending sweep. The creation mirror is off under
    APP_ENV=testing (``AgentStudioService._mirror`` only runs when the
    worker kill switch is on), so the sweep is what mirrors a Studio run
    in tests — same pattern as tests/test_agent_studio_unified.py."""
    from app.agent_studio.unified import mirror_pending_studio_runs
    from app.main import app

    return client.portal.call(
        mirror_pending_studio_runs, app.state.sessionmaker, app.state.cipher
    )


def _studio_run_status(studio_run_id):
    async def query(conn):
        return (await conn.execute(
            text("SELECT status FROM agent_studio_runs WHERE id = :id"), {"id": str(studio_run_id)}
        )).scalar_one()
    return run_db(query)


def _paused_agent_via_overview(client, scope, agent_id):
    body = client.get(f"/api/control-room/scopes/{scope}/overview").json()
    agent = next(a for a in body["agents"] if a["id"] == agent_id)
    return agent["paused"]


def _pause_run_end_to_end(client, scope, run_id):
    """Manager requests, admin approves, manager executes a pause."""
    login(client, USER2_EMAIL, PASSWORD)
    approval = _request_run(client, scope, run_id, "pause")
    login(client, ADMIN_EMAIL, PASSWORD)
    _approve(client, approval)
    login(client, USER2_EMAIL, PASSWORD)
    response = _execute_run(client, scope, run_id, "pause", approval["id"])
    assert response.status_code == 200, response.text
    return response.json()


def _setup_run(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    draft = save(admin_client, scope)
    wid = publish(admin_client, scope, draft)
    run = trigger(admin_client, scope, wid)
    return scope, people, run


def test_run_pause_and_resume_need_exact_action_approval(admin_client):
    scope, people, run = _setup_run(admin_client)
    assert _run_status(run["id"]) == "queued"

    paused = _pause_run_end_to_end(admin_client, scope, run["id"])
    assert paused["status"] == "paused"

    # The same approval cannot be used twice.
    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_run(admin_client, scope, run["id"], "resume")
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)
    login(admin_client, USER2_EMAIL, PASSWORD)
    resumed = _execute_run(admin_client, scope, run["id"], "resume", approval["id"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"  # never started: back to queued, not re-run
    assert _run_status(run["id"]) == "queued"

    again = _execute_run(admin_client, scope, run["id"], "resume", approval["id"])
    assert again.status_code == 409  # approval already used


def test_approval_for_one_action_cannot_run_another(admin_client):
    scope, people, run = _setup_run(admin_client)
    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_run(admin_client, scope, run["id"], "pause")
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)

    # The pause approval cannot resume the run.
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = _execute_run(admin_client, scope, run["id"], "resume", approval["id"])
    assert response.status_code == 409
    assert _run_status(run["id"]) == "queued"


def test_requester_cannot_approve_their_own_control(admin_client):
    scope, people, run = _setup_run(admin_client)
    # The requester is the manager (USER2): _setup_run leaves the client
    # logged in as the admin, who is not a scope member and cannot request.
    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_run(admin_client, scope, run["id"], "pause")
    response = admin_client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "approve", "presented_hash": approval["action_hash"]},
    )
    assert response.status_code == 403


def test_plain_member_cannot_request_or_execute_controls(admin_client):
    scope, people, run = _setup_run(admin_client)
    login(admin_client, USER_EMAIL, PASSWORD)
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/runs/{run['id']}/control-request",
        json={"command": "pause"},
    )
    assert response.status_code == 403
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/runs/{run['id']}/control",
        json={"command": "pause", "approval_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403


def test_control_on_unknown_scope_or_run_is_404(admin_client):
    scope, people, run = _setup_run(admin_client)
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = admin_client.post(
        f"/api/control-room/scopes/{uuid.uuid4()}/runs/{run['id']}/control-request",
        json={"command": "pause"},
    )
    assert response.status_code == 404
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/runs/{uuid.uuid4()}/control-request",
        json={"command": "pause"},
    )
    assert response.status_code == 404


def test_pause_of_studio_run_pauses_mirror_and_resume_continues_it(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Verify this task", "control-room-two")
    assert queued.status_code == 202, queued.text
    studio_run_id = queued.json()["id"]

    assert _mirror_pending(admin_client) >= 1, "pending sweep mirrored nothing"
    mirror = _mirror_run(studio_run_id)
    assert mirror is not None, "studio run was not mirrored to the unified engine"
    assert mirror["status"] == "queued"

    _pause_run_end_to_end(admin_client, scope, mirror["id"])
    assert _run_status(mirror["id"]) == "paused"
    # The Studio run itself is untouched: no 'paused' status exists for it,
    # and pausing must not cancel or fail it.
    assert _studio_run_status(studio_run_id) == "queued"

    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_run(admin_client, scope, mirror["id"], "resume")
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)
    login(admin_client, USER2_EMAIL, PASSWORD)
    resumed = _execute_run(admin_client, scope, mirror["id"], "resume", approval["id"])
    assert resumed.status_code == 200, resumed.text
    assert _run_status(mirror["id"]) == "queued"
    assert _studio_run_status(studio_run_id) == "queued"


def test_agent_pause_and_resume_via_control_room(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    agent_id, _thread_id = _create_agent_and_thread(admin_client)
    assert _paused_agent_via_overview(admin_client, scope, agent_id) is False

    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_agent(admin_client, scope, agent_id, "pause")
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = _execute_agent(admin_client, scope, agent_id, "pause", approval["id"])
    assert response.status_code == 200, response.text
    assert response.json()["paused"] is True
    assert _paused_agent_via_overview(admin_client, scope, agent_id) is True

    approval = _request_agent(admin_client, scope, agent_id, "resume")
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = _execute_agent(admin_client, scope, agent_id, "resume", approval["id"])
    assert response.status_code == 200, response.text
    assert response.json()["paused"] is False


def test_agent_assign_transfers_ownership_to_scope_member(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    # The agent must start owned by someone other than the assignee:
    # setup_scope leaves the client logged in as the plain member (USER),
    # who is also the assign target below, so create it as the manager.
    login(admin_client, USER2_EMAIL, PASSWORD)
    agent_id, _thread_id = _create_agent_and_thread(admin_client)

    login(admin_client, USER2_EMAIL, PASSWORD)
    approval = _request_agent(admin_client, scope, agent_id, "assign", people[USER_EMAIL])
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    _approve(admin_client, approval)
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = _execute_agent(
        admin_client, scope, agent_id, "assign", approval["id"], people[USER_EMAIL]
    )
    assert response.status_code == 200, response.text
    assert response.json()["owner_id"] == people[USER_EMAIL]

    body = admin_client.get(f"/api/control-room/scopes/{scope}/overview").json()
    agent = next(a for a in body["agents"] if a["id"] == agent_id)
    assert agent["owner_id"] == people[USER_EMAIL]


def test_agent_assign_to_outsider_and_master_agent_are_refused(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    agent_id, _thread_id = _create_agent_and_thread(admin_client)

    async def seed_outsider(conn):
        from tests.conftest import _PASSWORD_HASH

        from app.db.models import User

        await conn.execute(User.__table__.insert().values(
            email=OUTSIDER_EMAIL, role="user", password_hash=_PASSWORD_HASH))
    run_db(seed_outsider)

    async def _outsider_id(conn):
        return (await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": OUTSIDER_EMAIL}
        )).scalar_one()
    outsider_id = run_db(_outsider_id)

    login(admin_client, USER2_EMAIL, PASSWORD)
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/agents/{agent_id}/control-request",
        json={"command": "assign", "assignee_id": str(outsider_id)},
    )
    assert response.status_code == 403  # not a member of this scope

    # The Master Agent belongs to its owner and cannot be reassigned.
    login(admin_client, ADMIN_EMAIL, PASSWORD)
    agents = admin_client.get("/api/agent-studio/agents").json()["agents"]
    master = next(a for a in agents if a["name"] == "Master Agent")
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/agents/{master['id']}/control-request",
        json={"command": "assign", "assignee_id": people[USER_EMAIL]},
    )
    assert response.status_code == 422  # InvalidInputError in this codebase


def test_assign_without_assignee_and_pause_with_assignee_are_refused(admin_client):
    scope, people = setup_scope(admin_client)
    _make_manager(scope, people)
    agent_id, _thread_id = _create_agent_and_thread(admin_client)
    login(admin_client, USER2_EMAIL, PASSWORD)
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/agents/{agent_id}/control-request",
        json={"command": "assign"},
    )
    # InvalidInputError maps to 422 in this codebase.
    assert response.status_code == 422
    response = admin_client.post(
        f"/api/control-room/scopes/{scope}/agents/{agent_id}/control-request",
        json={"command": "pause", "assignee_id": people[USER_EMAIL]},
    )
    assert response.status_code == 422


def test_health_ready_reports_workflow_engine_health(admin_client):
    scope, people, run = _setup_run(admin_client)
    response = admin_client.get("/health/ready")
    assert response.status_code == 200, response.text
    engine = response.json()["workflow_engine"]
    # Test runs with APP_ENV=testing, so the kill switch defaults off.
    assert engine["engine_on"] is False
    assert engine["status"] == "off"
    # Triggering a run wrote workflow events: the last recorded activity
    # is present, and the queued run is due.
    assert engine["last_tick_at"] is not None
    assert engine["due_runs"] >= 1


def test_health_ready_reports_engine_on_when_kill_switch_is_on(admin_client, monkeypatch):
    from app.core.config import Settings

    on = Settings(workflow_engine_worker_enabled=True)
    monkeypatch.setattr("app.api.routes.health.get_settings", lambda: on)
    monkeypatch.setattr("app.control_room.service.get_settings", lambda: on)
    response = admin_client.get("/health/ready")
    assert response.status_code == 200, response.text
    engine = response.json()["workflow_engine"]
    assert engine["engine_on"] is True
    assert engine["status"] == "on"
