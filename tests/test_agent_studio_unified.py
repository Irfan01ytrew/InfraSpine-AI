"""Gap 1 (step E): Agent Studio runs execute as real workflow runs on the unified runtime.

The WIP branch already wires the adapter (`app/agent_studio/unified.py`): runs are mirrored
to `origin='studio'` workflow runs, the engine executes them through the
`agent_studio.execute` handler, and the legacy loop leaves unified runs alone
(`exclude_unified`). These tests prove that wiring end to end, on a throwaway
`*_test` database only:

* a run created through the API is mirrored exactly once (creation mirror is off
  under APP_ENV=testing, so the engine tick's pending sweep mirrors it);
* the mirrored run executes through the handler to completion, with the attempt
  recorded once in the `execute_once` ledger;
* pausing / stopping the workflow run pauses / cancels the Agent Studio run,
  and resuming continues from the last verified step;
* a run waiting for its exact-action tool approval waits in the workflow step
  and continues after the decision;
* the handler refuses another owner's run, and a paused agent (step A) runs
  nothing through the unified path either.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.agent_studio.service import AgentStudioService, _encode_state, _seal, decode_run_state
from app.agent_studio.unified import StudioHost, configure_host
from app.core.config import get_settings
from app.db.agent_studio_models import AgentStudioApproval, AgentStudioRun
from app.db.models import User
from app.main import app
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, login, run_db
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue
from tests.test_finance_invoice_flow import pump
from tests.test_workflow_engine import setup_scope


def _stub_execute(monkeypatch):
    """Replace the LLM graph with a verified synthetic completion (as in the pause tests)."""
    executed: list[uuid.UUID] = []

    async def verified_execute(self, run, _agent, _thread, state, *, allow_retry=True,
                               expected_attempt, expected_claimed_at):
        executed.append(run.id)
        state["answer"] = "Verified synthetic result."
        run.status, run.failure_code, run.finished_at, run.claimed_at = "completed", None, datetime.now(UTC), None
        sealed = _encode_state(self.cipher, run.id, state)
        run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        await self.db.commit()
        return run, state, None

    monkeypatch.setattr(AgentStudioService, "_execute", verified_execute)
    return executed


@pytest.fixture
def studio_host(client):
    """Register this process as the Agent Studio execution host for the unified runtime."""
    configure_host(StudioHost(app.state.sessionmaker, get_settings(), app.state.cipher,
                              app.state.llm_runtime, app.state.rag))
    yield
    configure_host(None)


def _mirror_pending(client):
    from app.agent_studio.unified import mirror_pending_studio_runs
    return client.portal.call(mirror_pending_studio_runs, app.state.sessionmaker, app.state.cipher)


def _wf_runs(run_id):
    async def state(conn):
        rows = (await conn.execute(text(
            "SELECT r.id, r.status, r.phase, r.last_verified_node "
            "FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
            "WHERE r.origin = 'studio' AND r.origin_ref = :id"), {'id': str(run_id)})).mappings().all()
        return [dict(row) for row in rows]
    return run_db(state)


def _wf_step(run_id, node_id="agent"):
    async def state(conn):
        row = (await conn.execute(text(
            "SELECT s.status, s.node_id FROM wf_steps s JOIN wf_runs r ON r.id = s.run_id "
            "WHERE r.origin = 'studio' AND r.origin_ref = :id AND s.node_id = :node"),
            {'id': str(run_id), 'node': node_id})).mappings().first()
        return dict(row) if row else None
    return run_db(state)


def _wf_run_id(run_id):
    rows = _wf_runs(run_id)
    assert rows, "no unified workflow run mirrored this Agent Studio run"
    return rows[0]["id"]


def _studio_status(client, run_id):
    return client.get(f"/api/agent-studio/runs/{run_id}").json()["status"]


def _pump_until(client, run_id, want, limit=12):
    """Pump the engine until the studio run reaches `want` (or the limit is hit)."""
    for _ in range(limit):
        if _studio_status(client, run_id) == want:
            return True
        pump(client, 2)
    return _studio_status(client, run_id) == want


def _pump_until_wf(client, run_id, want="completed", limit=8):
    """Pump until the mirrored workflow run reaches `want`: the run finishes its end
    node a tick or two after the Agent Studio run itself completes."""
    for _ in range(limit):
        rows = _wf_runs(run_id)
        if rows and rows[0]["status"] == want:
            return True
        pump(client, 2)
    rows = _wf_runs(run_id)
    return bool(rows) and rows[0]["status"] == want


def _setup(client):
    """A department the admin belongs to, plus an agent, a thread and one queued run."""
    scope, people = setup_scope(client)
    login(client, ADMIN_EMAIL)
    agent_id, thread_id = _create_agent_and_thread(client)
    queued = _enqueue(client, agent_id, thread_id, "Verify this task", "unified-one")
    assert queued.status_code == 202, queued.text
    return scope, people, agent_id, thread_id, uuid.UUID(queued.json()["id"])


def _set_awaiting_approval(client, run_id):
    """Put the run behind an exact-action tool approval, as the pause tests do."""
    pending = {"name": "web_search", "arguments": {"query": "synthetic"}}

    async def wait_for_approval():
        async with app.state.sessionmaker() as db:
            run = await db.get(AgentStudioRun, run_id)
            state = decode_run_state(app.state.cipher, run)
            state.update(pending_tool=pending, awaiting_approval=True)
            sealed = _encode_state(app.state.cipher, run.id, state)
            run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
            run.status = "awaiting_approval"
            approval_id = uuid.uuid4()
            action = _seal(app.state.cipher, "approval-action", approval_id, json.dumps(pending))
            db.add(AgentStudioApproval(
                id=approval_id, run_id=run.id, owner_id=run.owner_id, tool_name="web_search",
                action_ciphertext=action.ciphertext, action_nonce=action.nonce, action_key_id=action.key_id,
            ))
            await db.commit()

    client.portal.call(wait_for_approval)


def test_creation_mirror_runs_when_workflow_engine_on(admin_client, clean_tables):
    """The production path: with the engine on, creating a run mirrors it at once,
    before any engine tick. (Under APP_ENV=testing the engine is off by default,
    so the other tests rely on the tick sweep instead.)"""
    from app.agent_studio.service import AgentStudioService
    from app.db.agent_studio_models import AgentStudioAgent
    scope, _, agent_id, thread_id, _ = _setup(admin_client)

    async def start_with_engine_on():
        async with app.state.sessionmaker() as db:
            agent = await db.get(AgentStudioAgent, uuid.UUID(agent_id))
            user = await db.get(User, agent.owner_id)
            settings = get_settings().model_copy(update={"workflow_engine_worker_enabled": True})
            service = AgentStudioService(db, user, settings, app.state.cipher,
                                         app.state.llm_runtime, app.state.rag)
            run, _, _ = await service.start_run(
                agent.id, uuid.UUID(thread_id), "Engine-on task", idempotency_key="engine-on-one")
            return run.id

    run_id = admin_client.portal.call(start_with_engine_on)
    rows = _wf_runs(run_id)
    assert len(rows) == 1
    assert rows[0]["status"] in {"queued", "running"}


def test_run_is_mirrored_exactly_once_by_the_engine_sweep(admin_client, clean_tables):
    scope, _, _, _, run_id = _setup(admin_client)
    assert _wf_runs(run_id) == []  # creation mirror is off under APP_ENV=testing
    pump(admin_client, 2)
    rows = _wf_runs(run_id)
    assert len(rows) == 1
    assert rows[0]["status"] in {"queued", "running"}
    assert _wf_step(run_id)["node_id"] == "agent"
    pump(admin_client, 3)
    assert len(_wf_runs(run_id)) == 1  # the sweep never mirrors the same run twice


def test_mirrored_run_executes_to_completion_through_execute_once(admin_client, clean_tables,
                                                                   monkeypatch, studio_host):
    executed = _stub_execute(monkeypatch)
    _, _, _, _, run_id = _setup(admin_client)
    assert _pump_until(admin_client, run_id, "completed")
    assert executed == [run_id]
    assert _pump_until_wf(admin_client, run_id)
    rows = _wf_runs(run_id)
    assert rows[0]["status"] == "completed" and rows[0]["phase"] == "done"
    assert rows[0]["last_verified_node"] == "agent"
    assert _wf_step(run_id)["status"] == "completed"

    async def executions(conn):
        result = await conn.execute(text(
            "SELECT status, COUNT(*) FROM platform_action_executions "
            "WHERE idempotency_key LIKE :key GROUP BY status"),
            {'key': f"agent_studio:{run_id}:attempt:%"})
        return [tuple(row) for row in result.all()]
    assert run_db(executions) == [("verified", 1)]  # one attempt, dispatched once, verified


def test_workflow_pause_stops_execution_and_resume_continues(admin_client, clean_tables,
                                                             monkeypatch, studio_host):
    executed = _stub_execute(monkeypatch)
    scope, _, _, _, run_id = _setup(admin_client)
    assert _mirror_pending(admin_client) == 1
    wf_id = _wf_run_id(run_id)
    login(admin_client, ADMIN_EMAIL)
    paused = admin_client.post(f"/api/workflow-engine/scopes/{scope}/runs/{wf_id}/control/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    pump(admin_client, 3)
    assert executed == [] and _studio_status(admin_client, run_id) == "queued"
    login(admin_client, ADMIN_EMAIL)
    resumed = admin_client.post(f"/api/workflow-engine/scopes/{scope}/runs/{wf_id}/control/resume")
    assert resumed.status_code == 200, resumed.text
    assert _pump_until(admin_client, run_id, "completed")
    assert executed == [run_id]
    assert _pump_until_wf(admin_client, run_id)
    assert _wf_runs(run_id)[0]["status"] == "completed"


def test_workflow_stop_cancels_a_queued_studio_run(admin_client, clean_tables, monkeypatch, studio_host):
    executed = _stub_execute(monkeypatch)
    scope, _, _, _, run_id = _setup(admin_client)
    assert _mirror_pending(admin_client) == 1
    wf_id = _wf_run_id(run_id)
    login(admin_client, ADMIN_EMAIL)
    stopped = admin_client.post(f"/api/workflow-engine/scopes/{scope}/runs/{wf_id}/control/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"
    assert _studio_status(admin_client, run_id) == "cancelled"
    pump(admin_client, 3)
    assert executed == []  # a stopped run is never executed afterwards
    assert _wf_runs(run_id)[0]["status"] == "cancelled"


def test_awaiting_approval_waits_then_completes_after_decision(admin_client, clean_tables,
                                                               monkeypatch, studio_host):
    executed = _stub_execute(monkeypatch)
    _, _, _, _, run_id = _setup(admin_client)
    _set_awaiting_approval(admin_client, run_id)
    assert _mirror_pending(admin_client) == 1
    pump(admin_client, 3)
    assert executed == [] and _studio_status(admin_client, run_id) == "awaiting_approval"
    assert _wf_step(run_id)["status"] == "waiting"
    decided = admin_client.post(f"/api/agent-studio/runs/{run_id}/decision",
                                json={"decision": "approve", "note": "Checked."})
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "queued"
    assert _pump_until(admin_client, run_id, "completed")
    assert executed == [run_id]


def test_handler_refuses_another_owners_run(admin_client, clean_tables, studio_host):
    scope, people, _, _, run_id = _setup(admin_client)
    assert _mirror_pending(admin_client) == 1
    wf_id = _wf_run_id(run_id)

    async def attempt_as_other():
        from app.platform.access import principal_for
        from app.platform.sync import group_scope_sync
        from app.workflows.actions import ActionContext
        from app.agent_studio.unified import AgentStudioExecuteHandler
        async with app.state.sessionmaker() as db:
            other = await db.get(User, uuid.UUID(people[USER2_EMAIL]))
            platform_scope = await db.run_sync(
                lambda s: group_scope_sync(s.connection(), uuid.UUID(scope)))

            async def _never():
                return None

            ctx = ActionContext(
                db=db, cipher=app.state.cipher, principal=principal_for(other, channel="workflow"),
                run_id=wf_id, step_id=uuid.uuid4(), group_id=uuid.UUID(scope),
                platform_scope_id=platform_scope, values={"studio_run_id": str(run_id)},
                inputs={}, checkpoint={}, should_stop=_never,
            )
            return await AgentStudioExecuteHandler().run(ctx)

    outcome = admin_client.portal.call(attempt_as_other)
    assert outcome.status == "failed" and outcome.failure_code == "agent_run_missing"
    assert _studio_status(admin_client, run_id) == "queued"  # the run itself is untouched


def test_legacy_loop_leaves_unified_runs_to_the_engine(admin_client, clean_tables, monkeypatch):
    from app.agent_studio.worker import process_next_agent_studio_run
    executed = _stub_execute(monkeypatch)
    _, _, _, _, run_id = _setup(admin_client)
    assert _mirror_pending(admin_client) == 1

    async def process(exclude_unified):
        return await process_next_agent_studio_run(
            app.state.sessionmaker, get_settings(), app.state.cipher,
            app.state.llm_runtime, app.state.rag, exclude_unified=exclude_unified)

    assert admin_client.portal.call(process, True) is False  # unified run: legacy loop skips it
    assert executed == []
    assert admin_client.portal.call(process, False) is True  # without the flag it would run it
    assert executed == [run_id]


def test_paused_agent_runs_nothing_through_the_unified_runtime(admin_client, clean_tables,
                                                               monkeypatch, studio_host):
    executed = _stub_execute(monkeypatch)
    scope, _, agent_id, _, run_id = _setup(admin_client)
    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/pause").json()["paused"] is True
    assert _mirror_pending(admin_client) == 1
    pump(admin_client, 3)
    assert executed == [] and _studio_status(admin_client, run_id) == "queued"
    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/resume").json()["paused"] is False
    assert _pump_until(admin_client, run_id, "completed")
    assert executed == [run_id]
