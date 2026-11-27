"""PostgreSQL/API acceptance for explicit agent-run content retention and audit behavior."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.services.agents import seal_run_value
from tests.conftest import ADMIN_EMAIL, CSRF, USER_EMAIL, login, run_db


def test_admin_preview_and_bounded_retention_purge_preserve_live_run_metadata(client):
    login(client, ADMIN_EMAIL)
    group = client.post(
        "/api/admin/access/groups",
        json={"name": f"Retention QA {uuid.uuid4().hex[:8]}", "kind": "department"},
    )
    assert group.status_code == 201, group.text
    department_id = group.json()["id"]
    profile_response = client.post(
        "/api/agents",
        json={
            "name": f"Retention QA agent {uuid.uuid4().hex[:8]}",
            "department_group_id": department_id,
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert profile_response.status_code == 201, profile_response.text
    profile_id = profile_response.json()["id"]
    profile_version = profile_response.json()["version"]
    actor_id = run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": ADMIN_EMAIL}))
    now = datetime.now(UTC)
    run_specs = [
        ("completed_old", "completed", now - timedelta(days=120)),
        ("failed_old", "failed", now - timedelta(days=100)),
        ("queued_old", "queued", now - timedelta(days=110)),
        ("running_old", "running", now - timedelta(days=105)),
        ("completed_recent", "completed", now - timedelta(days=5)),
    ]
    inserted = {}
    payloads = []
    for label, state, requested_at in run_specs:
        run_id = uuid.uuid4()
        inserted[label] = run_id
        task = seal_run_value(client.app.state.cipher, run_id, "task", f"synthetic task {label}")
        result = seal_run_value(
            client.app.state.cipher,
            run_id,
            "result",
            json.dumps(
                {
                    "answer": f"Synthetic result {label}.",
                    "insufficient_evidence": True,
                    "citations": [],
                    "retrieved_chunks": 0,
                    "strategy": "none",
                    "model": None,
                    "provider": None,
                }
            ),
        )
        payloads.append(
            {
                "id": run_id,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "department_id": department_id,
                "requester_id": actor_id,
                "status": state,
                "execution_mode": "queue" if state == "queued" else "sync",
                "requested_at": requested_at,
                "started_at": None if state == "queued" else requested_at,
                "finished_at": now if state in {"completed", "failed"} else None,
                "attempts": 0 if state == "queued" else 1,
                "task": task.ciphertext,
                "task_nonce": task.nonce,
                "task_key_id": task.key_id,
                "result": result.ciphertext,
                "result_nonce": result.nonce,
                "result_key_id": result.key_id,
            }
        )
    run_db(
        lambda conn: conn.execute(
            text(
                "INSERT INTO agent_profile_runs "
                "(id, profile_id, profile_version, department_group_id, requester_user_id, capability, "
                "task_ciphertext, task_nonce, task_key_id, result_ciphertext, result_nonce, result_key_id, "
                "status, execution_mode, attempts, requested_at, available_at, started_at, finished_at) "
                "VALUES (:id, :profile_id, :profile_version, :department_id, :requester_id, 'knowledge_search', "
                ":task, :task_nonce, :task_key_id, :result, :result_nonce, :result_key_id, "
                ":status, :execution_mode, :attempts, :requested_at, :requested_at, :started_at, :finished_at)"
            ),
            payloads,
        )
    )

    login(client, USER_EMAIL)
    denied = client.post(
        "/api/admin/agent-runs/retention/preview",
        json={"retention_days": 30},
        headers=CSRF,
    )
    assert denied.status_code == 403

    login(client, ADMIN_EMAIL)
    preview = client.post(
        "/api/admin/agent-runs/retention/preview",
        json={"retention_days": 30},
        headers=CSRF,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert preview.json()["eligible_runs"] == 2
    assert preview.json()["preserved_fields"]

    missing_confirmation = client.post(
        "/api/admin/agent-runs/retention/purge",
        json={"retention_days": 30, "batch_size": 1},
        headers=CSRF,
    )
    false_confirmation = client.post(
        "/api/admin/agent-runs/retention/purge",
        json={"retention_days": 30, "batch_size": 1, "confirm": False},
        headers=CSRF,
    )
    assert missing_confirmation.status_code == 422
    assert false_confirmation.status_code == 422

    first = client.post(
        "/api/admin/agent-runs/retention/purge",
        json={"retention_days": 30, "batch_size": 1, "confirm": True},
        headers=CSRF,
    )
    assert first.status_code == 200, first.text
    assert first.json()["purged_runs"] == 1
    assert first.json()["remaining_eligible_runs"] == 1

    params = {f"run_{index}": run_id for index, run_id in enumerate(inserted.values())}
    placeholders = " OR ".join(f"id=:run_{index}" for index in range(len(params)))
    async def read_run_state(conn):
        result = await conn.execute(
            text(
                "SELECT id, status, task_ciphertext IS NULL AS task_cleared, "
                "result_ciphertext IS NULL AS result_cleared, content_purged_at "
                f"FROM agent_profile_runs WHERE {placeholders}"
            ),
            params,
        )
        return result.all()

    state = run_db(read_run_state)
    by_id = {row.id: row for row in state}
    first_purged = next(run_id for run_id in (inserted["completed_old"], inserted["failed_old"]) if by_id[run_id].task_cleared)
    assert by_id[first_purged].result_cleared is True
    assert by_id[first_purged].content_purged_at is not None
    assert by_id[first_purged].status in {"completed", "failed"}
    assert by_id[inserted["queued_old"]].task_cleared is False
    assert by_id[inserted["running_old"]].task_cleared is False
    assert by_id[inserted["completed_recent"]].task_cleared is False

    history = client.get(f"/api/agents/{profile_id}/runs")
    assert history.status_code == 200, history.text
    redacted = next(item for item in history.json()["runs"] if item["run_id"] == str(first_purged))
    assert redacted["content_purged_at"] is not None
    assert redacted["result"] is None
    assert redacted["retry_available"] is False

    second = client.post(
        "/api/admin/agent-runs/retention/purge",
        json={"retention_days": 30, "batch_size": 10, "confirm": True},
        headers=CSRF,
    )
    assert second.status_code == 200, second.text
    assert second.json()["purged_runs"] == 1
    assert second.json()["remaining_eligible_runs"] == 0
    history_after = client.get(f"/api/agents/{profile_id}/runs")
    assert history_after.status_code == 200
    assert all(
        item["content_purged_at"] is not None and item["result"] is None and not item["retry_available"]
        for item in history_after.json()["runs"]
        if item["run_id"] in {str(inserted["completed_old"]), str(inserted["failed_old"])}
    )
    audit_count = run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM audit_logs WHERE action='agent.profile_run_content_purged'")
        )
    )
    assert audit_count == 2
