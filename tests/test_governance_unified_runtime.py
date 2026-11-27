"""Step E gap 1 (governance): agent action proposals run as real workflow runs.

A department proposal is mirrored as one `origin='governance'` run on the unified
engine: an approval step waits for the independent human decision, then a
permissioned execute step runs the action through `execute_once` with read-back.
The legacy `/api/agents/actions/proposals/{id}/execute` API uses the same execution
path, and a paused or stopped run blocks execution through either path. Proposals
without a department keep the legacy path (the runtime is department scoped).
"""

import uuid

from sqlalchemy import text

from app.db.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    Document,
    User,
)
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


def _private_document() -> str:
    admin_id = uuid.UUID(_user_id(ADMIN_EMAIL))

    async def create(conn):
        return await conn.scalar(
            Document.__table__.insert().values(
                owner_id=admin_id, visibility=VISIBILITY_PRIVATE,
                filename="synthetic-governance-publish-test.pdf", file_format="pdf",
                storage_key=uuid.uuid4().hex * 2, sha256="c" * 64, size_bytes=1, status="ready",
            ).returning(Document.id)
        )

    return str(run_db(create))


def _setup(client, *, with_document=True):
    """Department with employee (requester, member), manager and admin; returns ids."""
    login(client, ADMIN_EMAIL)
    department_id = _department(client, f"Governance {uuid.uuid4().hex[:6]}")
    manager_email = f"gov_manager_{uuid.uuid4().hex[:8]}@example.com"
    manager_id = _create_user(manager_email, ROLE_MANAGER)
    _add_manager(department_id, manager_id)
    _add_member(department_id, manager_id)
    _add_member(department_id, _user_id(USER_EMAIL))
    document_id = _private_document() if with_document else None
    return {"department_id": department_id, "manager_email": manager_email,
            "manager_id": manager_id, "document_id": document_id}


def _propose(client, department_id, document_id=None, *, action_type="document_publish"):
    login(client, USER_EMAIL)
    parameters = {"document_id": document_id} if document_id else {
        "url": "https://api.example.com/events", "payload": {"event": "test"}}
    response = client.post(
        "/api/agents/actions/proposals",
        json={"action_type": action_type, "parameters": parameters,
              "rationale": "Governance runtime test proposal",
              "idempotency_key": f"gov-{uuid.uuid4()}", "department_id": department_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _approve(client, manager_email, proposal_id):
    login(client, manager_email)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/approve",
                           json={"rationale": "Independent authorization granted"})
    assert response.status_code == 200, response.text
    return response.json()


def _wf_run(proposal_id):
    async def query(conn):
        return (await conn.execute(text(
            "SELECT r.id, r.status, r.phase, r.last_verified_node, r.failure_code "
            "FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
            "WHERE r.origin = 'governance' AND r.origin_ref = :id"), {"id": proposal_id})).all()

    return run_db(query)


def _wf_steps(run_id):
    async def query(conn):
        return (await conn.execute(text(
            "SELECT node_id, status, verified_at IS NOT NULL FROM wf_steps WHERE run_id = :id "
            "ORDER BY node_id"), {"id": run_id})).all()

    return run_db(query)


def _proposal_state(proposal_id):
    async def query(conn):
        return (await conn.execute(text(
            "SELECT status, execution_result FROM agent_action_proposals WHERE id = :id"),
            {"id": proposal_id})).one()

    return run_db(query)


def _verified_executions(proposal_id):
    async def query(conn):
        return (await conn.execute(text(
            "SELECT count(*) FROM platform_action_executions "
            "WHERE subject = :subject AND status = 'verified'"),
            {"subject": f"agent_action:{proposal_id}"})).scalar()

    return run_db(query)


def _control(client, department_id, run_id, command, *, email=USER_EMAIL):
    login(client, email)
    response = client.post(
        f"/api/workflow-engine/scopes/{department_id}/runs/{run_id}/control/{command}")
    return response


def test_proposal_mirrored_as_governance_run_and_executes_via_workflow(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    # The engine is off at propose time in tests, so no run exists until a tick mirrors it.
    assert _wf_run(proposal_id) == []
    pump(client, 6)
    runs = _wf_run(proposal_id)
    assert len(runs) == 1, runs
    run_id, status, phase, last_verified, failure = runs[0]
    assert status in {"queued", "running"} and failure is None
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"][0] == "waiting"  # waits for the independent human decision
    assert steps["execute"][0] == "blocked"
    # No duplicate mirror on further ticks.
    pump(client, 2)
    assert len(_wf_run(proposal_id)) == 1

    _approve(client, ctx["manager_email"], proposal_id)
    pump(client, 6)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"] == ("completed", True)  # exact-action approval, verified by read-back
    assert steps["execute"][0] == "action"  # waits for someone holding workflow.manage

    # The requester (employee, workflow.access only) cannot take the execute step.
    login(client, USER_EMAIL)
    detail = client.get(f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    execute_step = next(s for s in detail.json()["steps"] if s["node_id"] == "execute")
    response = client.post(
        f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/steps/{execute_step['id']}/perform")
    assert response.status_code in {403, 404}, response.text

    # An administrator holds workflow.manage: perform, then the engine executes once.
    login(client, ADMIN_EMAIL)
    response = client.post(
        f"/api/workflow-engine/scopes/{ctx['department_id']}/runs/{run_id}/steps/{execute_step['id']}/perform")
    assert response.status_code == 200, response.text
    pump(client, 6)
    runs = _wf_run(proposal_id)
    assert runs[0][1] == "completed" and runs[0][2] == "done" and runs[0][3] == "execute"
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["execute"] == ("completed", True)
    state = _proposal_state(proposal_id)
    assert state.status == "executed" and state.execution_result["visibility"] == VISIBILITY_SHARED
    assert _verified_executions(proposal_id) == 1
    visibility = run_db(lambda conn: conn.scalar(
        text("SELECT visibility FROM documents WHERE id = :id"), {"id": ctx["document_id"]}))
    assert visibility == VISIBILITY_SHARED

    # Re-executing through the legacy API re-verifies instead of running the action again.
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    assert _verified_executions(proposal_id) == 1
    assert len(_wf_run(proposal_id)) == 1


def test_execute_via_legacy_api_uses_same_execute_once_path(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    pump(client, 6)
    run_id = _wf_run(proposal_id)[0][0]
    _approve(client, ctx["manager_email"], proposal_id)
    pump(client, 3)
    # Legacy execute path: same execute_once execution, then the workflow step is taken
    # by the same person so the engine records and re-verifies the result.
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    pump(client, 6)
    runs = _wf_run(proposal_id)
    assert runs[0][1] == "completed" and runs[0][3] == "execute"
    assert _verified_executions(proposal_id) == 1
    state = _proposal_state(proposal_id)
    assert state.execution_result["visibility"] == VISIBILITY_SHARED


def test_pause_blocks_execution_and_resume_continues_from_last_verified_step(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    pump(client, 6)
    run_id = _wf_run(proposal_id)[0][0]
    assert _control(client, ctx["department_id"], run_id, "pause").status_code == 200
    assert _wf_run(proposal_id)[0][1] == "paused"
    # Approving while paused is recorded, but the run does not advance.
    _approve(client, ctx["manager_email"], proposal_id)
    pump(client, 3)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"][0] == "waiting"
    # Execution through the legacy API is refused while paused.
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 409, response.text
    assert _proposal_state(proposal_id).status == "approved"

    assert _control(client, ctx["department_id"], run_id, "resume").status_code == 200
    pump(client, 6)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"] == ("completed", True)  # last verified step kept, not re-decided
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 200, response.text
    pump(client, 6)
    assert _wf_run(proposal_id)[0][1] == "completed"
    assert _verified_executions(proposal_id) == 1


def test_stop_cancels_run_and_blocks_execution(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    pump(client, 6)
    run_id = _wf_run(proposal_id)[0][0]
    _approve(client, ctx["manager_email"], proposal_id)
    pump(client, 6)
    assert _wf_run(proposal_id)[0][1] == "running"
    assert _control(client, ctx["department_id"], run_id, "stop", email=ADMIN_EMAIL).status_code == 200
    assert _wf_run(proposal_id)[0][1] == "cancelled"
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 409, response.text
    state = _proposal_state(proposal_id)
    assert state.status == "approved" and state.execution_result is None  # never executed
    assert _verified_executions(proposal_id) == 0


def test_rejection_fails_run_and_resume_is_refused(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    pump(client, 6)
    run_id = _wf_run(proposal_id)[0][0]
    login(client, ctx["manager_email"])
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/reject",
                           json={"rationale": "Not approved for this department"})
    assert response.status_code == 200, response.text
    pump(client, 6)
    run_id, status, phase, last_verified, failure = _wf_run(proposal_id)[0]
    assert status == "failed" and failure == "approval_rejected"
    response = _control(client, ctx["department_id"], run_id, "resume")
    assert response.status_code == 409, response.text


def test_proposal_without_department_keeps_legacy_path(client):
    ctx = _setup(client, with_document=False)
    document_id = _private_document()
    login(client, USER_EMAIL)
    response = client.post(
        "/api/agents/actions/proposals",
        json={"action_type": "document_publish", "parameters": {"document_id": document_id},
              "rationale": "Organisation-wide proposal", "idempotency_key": f"gov-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    proposal_id = response.json()["id"]
    pump(client, 6)
    assert _wf_run(proposal_id) == []  # organisation-wide: no department scope for a run
    login(client, USER2_EMAIL)
    # USER2 is not a manager/admin: cannot approve.
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/approve",
                           json={"rationale": "Not my decision"})
    assert response.status_code == 403, response.text
    # The employee cannot approve their own proposal either.
    login(client, USER_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/approve",
                           json={"rationale": "Self approval"})
    assert response.status_code == 403, response.text
    second_admin = f"gov_admin2_{uuid.uuid4().hex[:6]}@example.com"
    _create_user(second_admin, ROLE_ADMIN)
    login(client, second_admin)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/approve",
                           json={"rationale": "Independent authorization granted"})
    assert response.status_code == 200, response.text
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    assert _wf_run(proposal_id) == []
    visibility = run_db(lambda conn: conn.scalar(
        text("SELECT visibility FROM documents WHERE id = :id"), {"id": document_id}))
    assert visibility == VISIBILITY_SHARED


def test_non_member_proposal_keeps_legacy_path(client):
    """A proposal from someone who is not a department member cannot be mirrored
    (the unified run needs a scope member as requester), so it keeps the legacy
    path and still executes through the same adapters once approved."""
    ctx = _setup(client)
    document_id = _private_document()
    # USER2 is not a member of this department.
    login(client, USER2_EMAIL)
    response = client.post(
        "/api/agents/actions/proposals",
        json={"action_type": "document_publish", "parameters": {"document_id": document_id},
              "rationale": "Proposal from outside the department",
              "idempotency_key": f"gov-{uuid.uuid4()}", "department_id": ctx["department_id"]},
    )
    assert response.status_code == 201, response.text
    proposal_id = response.json()["id"]
    pump(client, 6)
    assert _wf_run(proposal_id) == []
    _approve(client, ctx["manager_email"], proposal_id)
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    assert _wf_run(proposal_id) == []
    visibility = run_db(lambda conn: conn.scalar(
        text("SELECT visibility FROM documents WHERE id = :id"), {"id": document_id}))
    assert visibility == VISIBILITY_SHARED


def test_execute_before_approval_is_refused_and_run_stays_waiting(client):
    ctx = _setup(client)
    proposal_id = _propose(client, ctx["department_id"], ctx["document_id"])
    pump(client, 6)
    run_id = _wf_run(proposal_id)[0][0]
    login(client, ADMIN_EMAIL)
    response = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert response.status_code == 409, response.text
    pump(client, 3)
    steps = dict((node, (s, verified)) for node, s, verified in _wf_steps(run_id))
    assert steps["approval"][0] == "waiting" and steps["execute"][0] == "blocked"
    assert _wf_run(proposal_id)[0][1] in {"queued", "running"}
