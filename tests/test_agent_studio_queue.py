from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select

from app.agent_studio.service import AgentStudioService, _encode_state, decode_run_state
from app.agent_studio.worker import process_next_agent_studio_run, recover_abandoned_agent_studio_runs
from app.core.config import get_settings
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun, AgentStudioThread
from app.db.models import User
from app.main import app


def _create_agent_and_thread(client):
    response = client.post("/api/agent-studio/agents", json={
        "name": "Queue test agent",
        "instructions": "Return a verified result for the task.",
        "tool_permissions": ["web_search"],
        "approval_tools": [],
        "delegate_agent_ids": [],
        "rag_enabled": False,
        "max_steps": 2,
    })
    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]
    thread = client.post(f"/api/agent-studio/agents/{agent_id}/threads", json={"title": "Queue acceptance"})
    assert thread.status_code == 201, thread.text
    return agent_id, thread.json()["id"]


def _enqueue(client, agent_id, thread_id, prompt, key):
    return client.post(
        f"/api/agent-studio/agents/{agent_id}/threads/{thread_id}/runs",
        json={"prompt": prompt},
        headers={"Idempotency-Key": key},
    )


def test_agent_studio_queue_idempotency_pause_resume_cancel_and_analytics(admin_client, clean_tables, monkeypatch):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    first = _enqueue(admin_client, agent_id, thread_id, "Verify this task", "queue-test-one")
    assert first.status_code == 202, first.text
    first_run = first.json()
    assert first_run["status"] == "queued"
    assert first_run["started_at"] is None

    duplicate = _enqueue(admin_client, agent_id, thread_id, "Verify this task", "queue-test-one")
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == first_run["id"]
    conflict = _enqueue(admin_client, agent_id, thread_id, "Different task", "queue-test-one")
    assert conflict.status_code == 409

    async def verified_execute(
        self,
        run,
        _agent,
        _thread,
        state,
        *,
        allow_retry=True,
        expected_attempt,
        expected_claimed_at,
    ):
        state["answer"] = "Verified synthetic result."
        run.status = "completed"
        run.failure_code = None
        run.finished_at = datetime.now(UTC)
        run.claimed_at = None
        sealed = _encode_state(self.cipher, run.id, state)
        run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        await self.db.commit()
        return run, state, None

    monkeypatch.setattr(AgentStudioService, "_execute", verified_execute)
    process_args = (
        app.state.sessionmaker,
        get_settings(),
        app.state.cipher,
        app.state.llm_runtime,
        app.state.rag,
    )
    assert admin_client.portal.call(process_next_agent_studio_run, *process_args) is True
    completed = admin_client.get(f"/api/agent-studio/runs/{first_run['id']}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["answer"] == "Verified synthetic result."

    paused_task = _enqueue(admin_client, agent_id, thread_id, "Run while paused", "queue-test-two")
    assert paused_task.status_code == 202
    paused_agent = admin_client.post(f"/api/agent-studio/agents/{agent_id}/pause")
    assert paused_agent.status_code == 200 and paused_agent.json()["paused"] is True
    assert admin_client.portal.call(process_next_agent_studio_run, *process_args) is False
    assert admin_client.get(f"/api/agent-studio/runs/{paused_task.json()['id']}").json()["status"] == "queued"
    resumed_agent = admin_client.post(f"/api/agent-studio/agents/{agent_id}/resume")
    assert resumed_agent.status_code == 200 and resumed_agent.json()["paused"] is False
    assert admin_client.portal.call(process_next_agent_studio_run, *process_args) is True
    assert admin_client.get(f"/api/agent-studio/runs/{paused_task.json()['id']}").json()["status"] == "completed"

    cancelled_task = _enqueue(admin_client, agent_id, thread_id, "Do not run", "queue-test-three")
    assert cancelled_task.status_code == 202
    cancelled = admin_client.post(f"/api/agent-studio/runs/{cancelled_task.json()['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert admin_client.portal.call(process_next_agent_studio_run, *process_args) is False

    report = admin_client.get("/api/agent-studio/analytics?days=7")
    assert report.status_code == 200, report.text
    totals = report.json()["totals"]
    assert totals["completed"] == 2
    assert totals["cancelled"] == 1
    assert totals["success_rate"] == 100.0
    assert totals["estimated_cost_usd"] is None
    assert report.json()["worker_enabled"] is False
    assert report.json()["system"]["available"] is True
    agent_metrics = next(item for item in report.json()["agents"] if item["id"] == agent_id)
    assert agent_metrics["total_runs"] == 3
    assert agent_metrics["completed"] == 2


def test_retry_classifier_only_retries_provider_or_network_failures():
    assert AgentStudioService._is_transient_error(TimeoutError())
    assert AgentStudioService._is_transient_error(ConnectionError())
    assert not AgentStudioService._is_transient_error(ValueError("invalid task"))


def test_recovered_run_fences_old_worker_claim(admin_client, clean_tables, monkeypatch):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Recover this task safely", "queue-stale-claim")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])
    settings = get_settings()
    old_claimed_at = datetime.now(UTC) - timedelta(
        seconds=settings.agent_studio_run_timeout_seconds + 120
    )

    async def prepare_expired_claim():
        async with app.state.sessionmaker() as db:
            run = await db.get(AgentStudioRun, run_id)
            assert run is not None
            run.status = "running"
            run.attempt_count = 1
            run.started_at = old_claimed_at
            run.claimed_at = old_claimed_at
            await db.commit()

    admin_client.portal.call(prepare_expired_claim)
    recovery_time = datetime.now(UTC) - timedelta(seconds=5)
    async def recover():
        return await recover_abandoned_agent_studio_runs(
            app.state.sessionmaker,
            settings,
            app.state.cipher,
            now=recovery_time,
        )

    recovered = admin_client.portal.call(recover)
    assert recovered == 1

    original_execute_claimed = AgentStudioService.execute_claimed

    async def stale_claim(self, claimed_run_id, *, expected_attempt, expected_claimed_at):
        return await original_execute_claimed(
            self,
            claimed_run_id,
            expected_attempt=1,
            expected_claimed_at=old_claimed_at,
        )

    monkeypatch.setattr(AgentStudioService, "execute_claimed", stale_claim)
    process_args = (
        app.state.sessionmaker,
        settings,
        app.state.cipher,
        app.state.llm_runtime,
        app.state.rag,
    )
    assert admin_client.portal.call(process_next_agent_studio_run, *process_args) is True

    async def stale_progress_attempt():
        async with app.state.sessionmaker() as db:
            current = await db.get(AgentStudioRun, run_id)
            assert current is not None
            assert current.status == "running"
            assert current.attempt_count == 2
            assert current.claimed_at is not None and current.claimed_at > old_claimed_at
            user = await db.get(User, current.owner_id)
            agent = await db.get(AgentStudioAgent, current.agent_id)
            thread = await db.get(AgentStudioThread, current.thread_id)
            assert user is not None and agent is not None and thread is not None
            service = AgentStudioService(
                db,
                user,
                settings,
                app.state.cipher,
                app.state.llm_runtime,
                app.state.rag,
            )
            result, _, _ = await service._execute(
                current,
                agent,
                thread,
                decode_run_state(app.state.cipher, current),
                expected_attempt=1,
                expected_claimed_at=old_claimed_at,
            )
            assert result.status == "running"
            assert result.attempt_count == 2

    admin_client.portal.call(stale_progress_attempt)

def test_scheduled_agent_waits_until_due(admin_client, clean_tables):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    path = f"/api/agent-studio/agents/{agent_id}/threads/{thread_id}/runs"
    scheduled = datetime.now(UTC) + timedelta(hours=1)
    body = {"prompt": "Prepare report", "scheduled_at": scheduled.isoformat()}
    headers = {"Idempotency-Key": "schedule-test"}
    first = admin_client.post(path, json=body, headers=headers)
    assert first.status_code == 202, first.text
    returned_time = datetime.fromisoformat(first.json()["next_attempt_at"].replace("Z", "+00:00"))
    assert returned_time == scheduled
    assert admin_client.post(path, json=body, headers=headers).json()["id"] == first.json()["id"]
    assert admin_client.post(path, json={"prompt": "Prepare report"}, headers=headers).status_code == 409
    assert admin_client.portal.call(process_next_agent_studio_run, app.state.sessionmaker,
        get_settings(), app.state.cipher, app.state.llm_runtime, app.state.rag) is False
    assert admin_client.post(f"/api/agent-studio/runs/{first.json()['id']}/cancel").json()["status"] == "cancelled"
