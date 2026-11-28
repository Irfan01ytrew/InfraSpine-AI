"""Step G slice 4 — continuity activations on the unified workflow runtime.

An approved continuity activation gets one workflow run
(`origin='continuity'`), the engine executes it through the
`continuity.execute` handler, ready steps with a bound workflow are
dispatched through `execute_once`, manual steps wait, and stopping the
workflow run suspends the activation. All on a throwaway `*_test`
database only, reusing the Module 08 acceptance fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.continuity.unified import mirror_pending_continuity_runs
from app.db.models import User
from app.main import app
from tests.conftest import USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_continuity import activation, approve, company, plan_input, post, scenario
from tests.test_finance_invoice_flow import pump


def _wf_runs(run_id):
    async def state(conn):
        rows = (await conn.execute(text(
            "SELECT r.id, r.status, r.phase FROM wf_runs r "
            "WHERE r.origin = 'continuity' AND r.origin_ref = :id"), {"id": str(run_id)})).mappings().all()
        return [dict(row) for row in rows]
    return run_db(state)


def _wf_run_id(run_id):
    rows = _wf_runs(run_id)
    assert rows, "no unified workflow run mirrored this continuity activation"
    return rows[0]["id"]


def _task_status(client, path, run_id):
    tasks = client.get(path + f"/runs/{run_id}").json()["tasks"]
    return {t["step_key"]: t["status"] for t in tasks}


def _executions(run_id):
    async def state(conn):
        rows = (await conn.execute(text(
            "SELECT idempotency_key, duty, status FROM platform_action_executions "
            "WHERE idempotency_key LIKE :key"), {"key": f"continuity:{run_id}:task:%"})).mappings().all()
        return [dict(row) for row in rows]
    return run_db(state)


def _approved_activation(client, c, workflow=True):
    """A plan + scenario + approved activation; returns (plan, run)."""
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    plan = post(client, c["path"] + "/plans", plan_input(c, workflow=workflow))
    approve(client, c["path"] + f"/plans/{plan['id']}/decision", plan)
    run = activation(client, c, plan, scene)
    approve(client, c["path"] + f"/runs/{run['id']}/decision", run)
    return plan, run


def test_activation_is_mirrored_exactly_once_at_decision(client, company):
    c = company
    _, run = _approved_activation(client, c)
    rows = _wf_runs(run["id"])
    assert len(rows) == 1
    assert rows[0]["status"] in {"queued", "running"}
    # The engine sweep finds nothing left to mirror.
    assert client.portal.call(mirror_pending_continuity_runs,
                              app.state.sessionmaker, app.state.cipher) == 0
    assert len(_wf_runs(run["id"])) == 1


def test_sweep_mirrors_an_active_run_missed_at_decision(client, company):
    # The mirror is append-only audit: a missed mirror is simulated by
    # making the decision-time mirror a no-op, not by deleting the run
    # (wf_events cannot be deleted — workflow evidence is append-only).
    import app.continuity.unified as unified
    from unittest.mock import patch

    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    plan = post(client, c["path"] + "/plans", plan_input(c, workflow=True))
    approve(client, c["path"] + f"/plans/{plan['id']}/decision", plan)
    run = activation(client, c, plan, scene)
    with patch.object(unified, "mirror_activation", return_value=None):
        approve(client, c["path"] + f"/runs/{run['id']}/decision", run)
    assert _wf_runs(run["id"]) == []
    assert client.portal.call(mirror_pending_continuity_runs, app.state.sessionmaker, app.state.cipher) == 1
    assert len(_wf_runs(run["id"])) == 1
    # A second sweep is a no-op (idempotent).
    assert client.portal.call(mirror_pending_continuity_runs, app.state.sessionmaker, app.state.cipher) == 0


def test_engine_dispatches_ready_task_once_through_execute_once(client, company):
    c = company
    _, run = _approved_activation(client, c)
    wf_id = _wf_run_id(run["id"])
    assert _task_status(client, c["path"], run["id"]) == {"handover": "pending"}
    pump(client, 6)
    assert _task_status(client, c["path"], run["id"]) == {"handover": "dispatched"}
    executions = _executions(run["id"])
    assert len(executions) == 1
    assert executions[0]["duty"] == "workflow.access"
    assert executions[0]["status"] == "verified"
    # Exactly one automation run was created: no double dispatch on re-pump.
    pump(client, 4)
    assert run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM automation_runs WHERE idempotency_key LIKE 'continuity:%'"))) == 1
    assert _task_status(client, c["path"], run["id"]) == {"handover": "dispatched"}
    # The workflow run is still waiting: the task needs its automation
    # approval + evidence + verification through the continuity API.
    assert _wf_runs(run["id"])[0]["status"] in {"running", "queued"}
    assert _wf_run_id(run["id"]) == wf_id


def test_engine_waits_on_a_manual_step(client, company):
    c = company
    _, run = _approved_activation(client, c, workflow=False)
    pump(client, 6)
    assert _task_status(client, c["path"], run["id"]) == {"handover": "pending"}
    assert _executions(run["id"]) == [], "a manual step must never be dispatched"
    assert _wf_runs(run["id"])[0]["status"] in {"running", "queued"}


def test_workflow_stop_suspends_the_activation(client, company):
    c = company
    _, run = _approved_activation(client, c)
    wf_id = _wf_run_id(run["id"])
    group_id = c["group"]["id"]
    stopped = client.post(f"/api/workflow-engine/scopes/{group_id}/runs/{wf_id}/control/stop")
    assert stopped.status_code == 200, stopped.text
    assert _wf_runs(run["id"])[0]["status"] == "cancelled"
    report = client.get(c["path"] + f"/runs/{run['id']}").json()
    assert report["run"]["status"] == "suspended"


def test_handler_refuses_another_owners_activation(client, company):
    from app.continuity.unified import ContinuityExecuteHandler
    from app.platform.access import principal_for
    from app.workflows.actions import ActionContext

    c = company
    _, run = _approved_activation(client, c)
    other_id = uuid.UUID(c["ids"][USER2_EMAIL])

    async def attempt_as_other():
        async with app.state.sessionmaker() as db:
            user = await db.get(User, other_id)
            handler = ContinuityExecuteHandler()

            async def never_stop():
                return None

            ctx = ActionContext(
                db=db, cipher=app.state.cipher,
                principal=principal_for(user, channel="workflow"),
                run_id=uuid.uuid4(), step_id=uuid.uuid4(),
                group_id=uuid.UUID(c["group"]["id"]),
                platform_scope_id=uuid.uuid4(),
                values={"continuity_run_id": str(run["id"])}, inputs={},
                checkpoint={}, should_stop=never_stop,
            )
            return await handler.run(ctx)

    outcome = client.portal.call(attempt_as_other)
    assert outcome.status == "failed"
    assert outcome.failure_code == "continuity_run_missing"
    assert _wf_runs(run["id"]), "the refusal must not delete the mirrored run"
