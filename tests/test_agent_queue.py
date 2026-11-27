"""Durable, fenced queue behavior for safe department agent runs."""

import asyncio
import uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.session import create_sessionmaker
from app.schemas.rag import RagChatResponse
from app.services.agent_worker import process_next_agent_run, recover_expired_agent_queue_runs
from tests.conftest import CSRF, TEST_DATABASE_URL, USER2_EMAIL, login, run_db
from tests.test_agent_profiles import _create_profile
from tests.test_automation import _manager_department


def _run_worker(client, *, retry_delay_seconds=0):
    async def _run():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            sessionmaker = create_sessionmaker(engine)
            return await process_next_agent_run(
                sessionmaker,
                get_settings(),
                client.app.state.rag,
                client.app.state.cipher,
                client.app.state.llm_runtime,
                retry_delay_seconds=retry_delay_seconds,
            )
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_queued_agent_run_is_encrypted_fenced_and_retries_transient_failure(client, monkeypatch):
    department, _employee_id = _manager_department(client, "Queued Agent Team")
    profile = _create_profile(client, department["id"])
    login(client, USER2_EMAIL)
    task = "Read the current recovery-token rotation procedure."

    rejected = client.post(
        f"/api/agents/{profile['id']}/queue",
        json={"capability": "public_web_research", "task": task},
        headers=CSRF,
    )
    assert rejected.status_code == 422

    queued_response = client.post(
        f"/api/agents/{profile['id']}/queue",
        json={"capability": "knowledge_search", "task": task},
        headers=CSRF,
    )
    assert queued_response.status_code == 202, queued_response.text
    queued = queued_response.json()
    assert queued["status"] == "queued" and queued["attempts"] == 0 and queued["started_at"] is None
    run_id = queued["run_id"]
    assert client.get(f"/api/agents/runs/{run_id}").json()["status"] == "queued"

    async def _stored_input(conn):
        return (
            await conn.execute(
                text("SELECT execution_mode, status, attempts, task_ciphertext FROM agent_profile_runs WHERE id=:id"),
                {"id": run_id},
            )
        ).one()

    stored = run_db(_stored_input)
    assert stored[:3] == ("queue", "queued", 0)
    assert task.encode() not in stored[3]

    calls = 0
    captured = {}

    async def flaky_rag_respond(_db, _user, _rag, _settings, _get_llm, question, **kwargs):
        nonlocal calls
        calls += 1
        captured.update(kwargs)
        assert question == task
        if calls == 1:
            raise RuntimeError("synthetic transient failure")
        return RagChatResponse(
            answer="Recovery tokens must be rotated every 90 days. [S1]",
            insufficient_evidence=False,
            citations=[],
            retrieved_chunks=1,
            strategy="semantic",
            model="test-model",
            provider="test-provider",
        )

    monkeypatch.setattr("app.services.agent_worker.rag_respond", flaky_rag_respond)
    assert _run_worker(client) is True
    intermediate = client.get(f"/api/agents/runs/{run_id}").json()
    assert intermediate["status"] == "queued" and intermediate["attempts"] == 1
    assert intermediate["retry_available"] is False
    assert _run_worker(client) is True

    completed = client.get(f"/api/agents/runs/{run_id}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed" and completed.json()["attempts"] == 2
    assert completed.json()["result"]["answer"] == "Recovery tokens must be rotated every 90 days. [S1]"
    assert captured["memory"] is None
    assert captured["department_group_id"] == uuid.UUID(department["id"])

    async def _events(conn):
        return (
            await conn.execute(
                text("SELECT action FROM audit_logs WHERE target=:id ORDER BY id"), {"id": run_id}
            )
        ).scalars().all()

    assert run_db(_events) == [
        "agent.profile_run_queued",
        "agent.profile_run_started",
        "agent.profile_run_attempt_failed",
        "agent.profile_run_started",
        "agent.profile_run_completed",
    ]
    async def _result_ciphertext(conn):
        row = await conn.execute(text("SELECT result_ciphertext FROM agent_profile_runs WHERE id=:id"), {"id": run_id})
        return row.scalar_one()
    assert b"Recovery tokens must be rotated" not in run_db(_result_ciphertext)


def test_queued_agent_run_fails_closed_after_membership_revocation(client, monkeypatch):
    department, employee_id = _manager_department(client, "Revoked Queue Team")
    profile = _create_profile(client, department["id"])
    login(client, USER2_EMAIL)
    queued = client.post(
        f"/api/agents/{profile['id']}/queue",
        json={"capability": "knowledge_search", "task": "Summarize the department policy."},
        headers=CSRF,
    )
    assert queued.status_code == 202, queued.text
    run_id = queued.json()["run_id"]

    login(client, "manager@example.com", "manager-password-123")
    removed = client.delete(f"/api/departments/{department['id']}/members/{employee_id}")
    assert removed.status_code == 204
    login(client, USER2_EMAIL)

    async def forbidden_rag(*_args, **_kwargs):
        raise AssertionError("A revoked employee's queued run must fail before retrieval.")

    monkeypatch.setattr("app.services.agent_worker.rag_respond", forbidden_rag)
    assert _run_worker(client) is True
    failed = client.get(f"/api/agents/runs/{run_id}")
    assert failed.status_code == 403

    async def _state(conn):
        return (
            await conn.execute(
                text("SELECT status, attempts, failure_code FROM agent_profile_runs WHERE id=:id"), {"id": run_id}
            )
        ).one()

    assert run_db(_state) == ("failed", 1, "PermissionDeniedError")


def test_expired_agent_queue_lease_is_requeued_once_then_failed(client):
    from app.services.agent_worker import recover_expired_agent_queue_runs

    department, _employee_id = _manager_department(client, "Expired Queue Team")
    profile = _create_profile(client, department["id"])
    login(client, USER2_EMAIL)
    run_ids = []
    for _ in range(2):
        response = client.post(
            f"/api/agents/{profile['id']}/queue",
            json={"capability": "knowledge_search", "task": "Summarize the department policy."},
            headers=CSRF,
        )
        assert response.status_code == 202
        run_ids.append(response.json()["run_id"])

    async def _expire(conn):
        await conn.execute(
            text(
                "UPDATE agent_profile_runs SET status='running', attempts=1, started_at=now()-interval '2 hours', "
                "locked_until=now()-interval '1 minute' WHERE id=:id"
            ),
            {"id": run_ids[0]},
        )
        await conn.execute(
            text(
                "UPDATE agent_profile_runs SET status='running', attempts=2, started_at=now()-interval '2 hours', "
                "locked_until=now()-interval '1 minute' WHERE id=:id"
            ),
            {"id": run_ids[1]},
        )

    run_db(_expire)

    async def _recover():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            return await recover_expired_agent_queue_runs(create_sessionmaker(engine))
        finally:
            await engine.dispose()

    assert asyncio.run(_recover()) == 2

    async def _states(conn):
        return (
            await conn.execute(
                text("SELECT id::text, status, attempts FROM agent_profile_runs WHERE id=ANY(:ids) ORDER BY id"),
                {"ids": run_ids},
            )
        ).all()

    values = {run_id: (status, attempts) for run_id, status, attempts in run_db(_states)}
    assert values[run_ids[0]] == ("queued", 1)
    assert values[run_ids[1]] == ("failed", 2)
