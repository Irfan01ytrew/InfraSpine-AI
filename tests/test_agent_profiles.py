"""Synthetic acceptance coverage for versioned department-scoped agent profiles."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.errors import ConflictError
from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, login, run_db
from tests.test_automation import _manager_department


def _create_profile(client, department_id):
    response = client.post(
        "/api/agents",
        json={
            "name": "Operations knowledge researcher",
            "department_group_id": department_id,
            "agent_type": "knowledge_research",
            "knowledge_scope": "department_only",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_agent_catalog_is_department_scoped_and_employee_is_read_only(client):
    department, _employee_id = _manager_department(client, "Agent Operations")
    profile = _create_profile(client, department["id"])
    assert profile["version"] == 1 and profile["can_manage"] is True
    assert profile["tool_policy"] == ["knowledge_search"]
    assert profile["knowledge_scope"] == "department_only"

    login(client, USER2_EMAIL)
    listed = client.get("/api/agents")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["agents"]] == [profile["id"]]
    assert listed.json()["agents"][0]["can_manage"] is False
    denied = client.post(
        "/api/agents",
        json={
            "name": "Employee-owned profile",
            "department_group_id": department["id"],
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert denied.status_code == 403


def test_agent_versions_are_immutable_and_old_version_cannot_be_reenabled(client):
    department, _employee_id = _manager_department(client, "Agent Versioning")
    original = _create_profile(client, department["id"])
    versioned = client.put(
        f"/api/agents/{original['id']}",
        json={
            "expected_version": 1,
            "name": "Operations researcher v2",
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert versioned.status_code == 201, versioned.text
    assert versioned.json()["version"] == 2 and versioned.json()["enabled"] is True

    rows = client.get("/api/agents").json()["agents"]
    assert [(row["version"], row["enabled"]) for row in rows] == [(1, False), (2, True)]
    with pytest.raises(Exception, match="disabled agent profile versions cannot be re-enabled"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE agent_profiles SET enabled = true WHERE id = :id"), {"id": original["id"]}
            )
        )

    disabled = client.post(f"/api/agents/{versioned.json()['id']}/disable", headers=CSRF)
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    login(client, USER2_EMAIL)
    assert client.get("/api/agents").json()["agents"] == []


def test_agent_capabilities_are_allowlisted_for_agent_type(client):
    department, _employee_id = _manager_department(client, "Agent Tool Policy")
    rejected = client.post(
        "/api/agents",
        json={
            "name": "Unsafe tool selection",
            "department_group_id": department["id"],
            "agent_type": "employee_training",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["public_web_research"],
        },
        headers=CSRF,
    )
    assert rejected.status_code == 422

    # Even valid profiles cannot be written to departments the acting manager does not manage.
    login(client, ADMIN_EMAIL)
    from tests.test_enterprise_people import _department

    other = _department(client, "Agent Restricted Department")
    login(client, "manager@example.com", "manager-password-123")
    denied = client.post(
        "/api/agents",
        json={
            "name": "Cross department profile",
            "department_group_id": other["id"],
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert denied.status_code == 403


def test_agent_run_uses_server_department_scope_and_records_audit(client, monkeypatch):
    department, _employee_id = _manager_department(client, "Agent Run Boundary")
    profile = _create_profile(client, department["id"])
    login(client, USER2_EMAIL)
    captured = {}

    async def fake_rag_respond(*_args, **kwargs):
        captured.update(kwargs)
        from app.schemas.rag import RagChatResponse

        return RagChatResponse(
            answer="A scoped test answer.",
            insufficient_evidence=False,
            citations=[],
            retrieved_chunks=0,
            strategy="semantic",
            model="test-model",
            provider="test-provider",
        )

    monkeypatch.setattr("app.api.routes.agents.rag_respond", fake_rag_respond)
    response = client.post(
        f"/api/agents/{profile['id']}/runs",
        json={"capability": "knowledge_search", "task": "Summarize the department procedure."},
        headers=CSRF,
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["answer"] == "A scoped test answer."
    assert captured["department_group_id"] == uuid.UUID(department["id"])
    assert captured["memory"] is None
    run_id = response.json()["run_id"]

    async def _read_events(conn):
        result = await conn.execute(
            text("SELECT action, outcome, details::text FROM audit_logs WHERE target = :target ORDER BY id"),
            {"target": run_id},
        )
        return result.all()

    events = run_db(_read_events)
    assert [event[0] for event in events] == ["agent.profile_run_started", "agent.profile_run_completed"]
    assert all(event[1] == "success" for event in events)
    assert "Summarize the department procedure" not in " ".join(event[2] for event in events)


def test_agent_run_is_encrypted_retrievable_retryable_once_and_revoked_with_membership(client, monkeypatch):
    department, employee_id = _manager_department(client, "Agent Durable Runs")
    profile = _create_profile(client, department["id"])
    login(client, USER2_EMAIL)
    task = "Private maintenance schedule for team Delta"
    calls = 0

    async def flaky_rag_respond(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["department_group_id"] == uuid.UUID(department["id"])
        assert kwargs["memory"] is None
        if calls == 1:
            raise ConflictError("Synthetic transient failure details are not retained.")
        from app.schemas.rag import RagChatResponse

        return RagChatResponse(
            answer="Private result for team Delta.",
            insufficient_evidence=False,
            citations=[],
            retrieved_chunks=0,
            strategy="semantic",
            model="test-model",
            provider="test-provider",
        )

    monkeypatch.setattr("app.api.routes.agents.rag_respond", flaky_rag_respond)
    failed = client.post(
        f"/api/agents/{profile['id']}/runs",
        json={"capability": "knowledge_search", "task": task},
        headers=CSRF,
    )
    assert failed.status_code == 409, failed.text

    async def _load_run(conn):
        row = await conn.execute(
            text(
                "SELECT id, status, attempts, task_ciphertext, result_ciphertext, failure_code "
                "FROM agent_profile_runs WHERE requester_user_id = "
                "(SELECT id FROM users WHERE email = :email) ORDER BY requested_at DESC LIMIT 1"
            ),
            {"email": USER2_EMAIL},
        )
        return row.one()

    stored = run_db(_load_run)
    run_id = str(stored[0])
    assert stored[1:] == ("failed", 1, stored[3], None, "ConflictError")
    assert task.encode() not in stored[3]
    failure_history = client.get(f"/api/agents/runs/{run_id}")
    assert failure_history.status_code == 200, failure_history.text
    assert failure_history.json()["status"] == "failed"
    assert failure_history.json()["attempts"] == 1
    assert failure_history.json()["retry_available"] is True
    assert "Private maintenance schedule" not in failure_history.text
    assert client.get(f"/api/agents/{profile['id']}/runs").json()["runs"][0]["run_id"] == run_id

    async def _simulate_interrupted_process(conn):
        await conn.execute(
            text(
                "UPDATE agent_profile_runs SET status = 'running', failure_code = NULL, finished_at = NULL, "
                "started_at = now() - interval '31 minutes' WHERE id = :id"
            ),
            {"id": run_id},
        )

    run_db(_simulate_interrupted_process)
    interrupted = client.get(f"/api/agents/runs/{run_id}")
    assert interrupted.status_code == 200
    assert interrupted.json()["status"] == "running"
    assert interrupted.json()["retry_available"] is True

    retried = client.post(f"/api/agents/runs/{run_id}/retry", headers=CSRF)
    assert retried.status_code == 200, retried.text
    assert retried.json()["result"]["answer"] == "Private result for team Delta."
    saved = client.get(f"/api/agents/runs/{run_id}")
    assert saved.status_code == 200
    assert saved.json()["status"] == "completed"
    assert saved.json()["attempts"] == 2
    assert saved.json()["retry_available"] is False
    assert saved.json()["result"]["answer"] == "Private result for team Delta."
    assert client.get(f"/api/agents/{profile['id']}/runs").json()["runs"][0]["result"]["answer"] == "Private result for team Delta."

    async def _ciphertexts(conn):
        result = await conn.execute(
            text("SELECT task_ciphertext, result_ciphertext FROM agent_profile_runs WHERE id = :id"),
            {"id": run_id},
        )
        return result.one()

    encrypted_task, encrypted_result = run_db(_ciphertexts)
    assert task.encode() not in encrypted_task
    assert b"Private result for team Delta" not in encrypted_result
    assert calls == 2

    blocked_duplicate = client.post(f"/api/agents/runs/{run_id}/retry", headers=CSRF)
    assert blocked_duplicate.status_code == 409

    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert client.get(f"/api/agents/runs/{run_id}").status_code == 403

    async def _events(conn):
        rows = await conn.execute(
            text("SELECT action, outcome FROM audit_logs WHERE target = :target ORDER BY id"),
            {"target": run_id},
        )
        return rows.all()

    events = run_db(_events)
    assert [row[0] for row in events] == [
        "agent.profile_run_started",
        "agent.profile_run_failed",
        "agent.profile_run_interrupted",
        "agent.profile_run_retry_started",
        "agent.profile_run_completed",
    ]
    assert [row[1] for row in events] == ["success", "failure", "failure", "success", "success"]


def test_api_startup_marks_only_stale_agent_runs_interrupted(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    department, employee_id = _manager_department(client, "Agent Startup Recovery")
    profile = _create_profile(client, department["id"])
    now = datetime.now(timezone.utc)
    stale_id, recent_id, completed_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        {
            "id": stale_id,
            "profile_id": uuid.UUID(profile["id"]),
            "profile_version": 1,
            "department_group_id": uuid.UUID(department["id"]),
            "requester_user_id": employee_id,
            "capability": "knowledge_search",
            "task_ciphertext": b"encrypted-task-placeholder",
            "task_nonce": b"nonce-placeholder",
            "task_key_id": "test-v1",
            "status": "running",
            "attempts": 1,
            "requested_at": now - timedelta(minutes=31),
            "started_at": now - timedelta(minutes=31),
            "finished_at": None,
        },
        {
            "id": recent_id,
            "profile_id": uuid.UUID(profile["id"]),
            "profile_version": 1,
            "department_group_id": uuid.UUID(department["id"]),
            "requester_user_id": employee_id,
            "capability": "knowledge_search",
            "task_ciphertext": b"encrypted-task-placeholder",
            "task_nonce": b"nonce-placeholder",
            "task_key_id": "test-v1",
            "status": "running",
            "attempts": 1,
            "requested_at": now - timedelta(minutes=5),
            "started_at": now - timedelta(minutes=5),
            "finished_at": None,
        },
        {
            "id": completed_id,
            "profile_id": uuid.UUID(profile["id"]),
            "profile_version": 1,
            "department_group_id": uuid.UUID(department["id"]),
            "requester_user_id": employee_id,
            "capability": "knowledge_search",
            "task_ciphertext": b"encrypted-task-placeholder",
            "task_nonce": b"nonce-placeholder",
            "task_key_id": "test-v1",
            "status": "completed",
            "attempts": 1,
            "requested_at": now - timedelta(hours=2),
            "started_at": now - timedelta(hours=2),
            "finished_at": now - timedelta(hours=1),
        },
    ]
    run_db(lambda conn: conn.execute(text(
        "INSERT INTO agent_profile_runs "
        "(id, profile_id, profile_version, department_group_id, requester_user_id, capability, "
        "task_ciphertext, task_nonce, task_key_id, status, attempts, requested_at, started_at, finished_at) "
        "VALUES (:id, :profile_id, :profile_version, :department_group_id, :requester_user_id, :capability, "
        ":task_ciphertext, :task_nonce, :task_key_id, :status, :attempts, :requested_at, :started_at, :finished_at)"
    ), rows))

    # A fresh application lifespan represents a process restart and runs the bounded recovery sweep.
    with TestClient(create_app()):
        pass

    async def read_recovered_state(conn):
        state = await conn.execute(
            text("SELECT id, status, failure_code FROM agent_profile_runs WHERE id IN (:stale, :recent, :completed)"),
            {"stale": stale_id, "recent": recent_id, "completed": completed_id},
        )
        audit = await conn.execute(
            text(
                "SELECT actor_user_id, actor_email, details->>'attempt' FROM audit_logs "
                "WHERE action = 'agent.profile_run_interrupted' AND target = :target"
            ),
            {"target": str(stale_id)},
        )
        return {row.id: (row.status, row.failure_code) for row in state}, audit.all()

    state, events = run_db(read_recovered_state)
    assert state == {
        stale_id: ("failed", "interrupted"),
        recent_id: ("running", None),
        completed_id: ("completed", None),
    }
    assert len(events) == 1
    assert events[0][0] is None and events[0][1] == "system:agent-run-recovery" and events[0][2] == "1"


def test_agent_run_requires_enabled_capability_and_external_consent(client):
    department, _employee_id = _manager_department(client, "Agent Run Capability")
    profile = _create_profile(client, department["id"])
    rejected_tool = client.post(
        f"/api/agents/{profile['id']}/runs",
        json={"capability": "public_web_research", "task": "Competitor landscape", "confirm_external_search": True},
        headers=CSRF,
    )
    assert rejected_tool.status_code == 403

    login(client, "manager@example.com", "manager-password-123")
    market = client.post(
        "/api/agents",
        json={
            "name": "Market research",
            "department_group_id": department["id"],
            "agent_type": "market_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["public_web_research"],
        },
        headers=CSRF,
    )
    assert market.status_code == 201, market.text
    missing_consent = client.post(
        f"/api/agents/{market.json()['id']}/runs",
        json={"capability": "public_web_research", "task": "Competitor landscape"},
        headers=CSRF,
    )
    assert missing_consent.status_code == 422
