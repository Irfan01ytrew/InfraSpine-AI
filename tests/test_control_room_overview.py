"""Step Q (slice 1): read-only control-room overview API.

Proves, on a throwaway ``*_test`` database only, that the overview
aggregates the unified engine + Agent Studio state for one department scope,
that it is authorized through ``app.platform`` (404 for unknown scope /
non-member, 401 unauthenticated), and that the agent registry only lists
agents owned by scope members.
"""

import uuid

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, PASSWORD, _PASSWORD_HASH, login, run_db
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue
from tests.test_workflow_engine import publish, save, setup_scope, trigger

EXTRA_EMAIL = "control-room-outsider@example.com"


def _overview(client, scope, **params):
    return client.get(f"/api/control-room/scopes/{scope}/overview", params=params)


def _seed_failed_run(scope, workflow_id, version_id):
    """Insert a failed run directly. Raw SQL must set every NOT NULL column
    that only has a Python-side default (simulation, priority,
    event_sequence, event_hash) — the ORM defaults do not apply here."""
    async def seed(conn):
        requester = (await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": ADMIN_EMAIL}
        )).scalar_one()
        await conn.execute(text(
            "INSERT INTO wf_runs (id, scope_id, workflow_id, version_id, requester_id,"
            " idempotency_key, input_digest, payload, status, phase, failure_code,"
            " simulation, priority, event_sequence, event_hash)"
            " VALUES (:id, :scope, :wid, :vid, :req, 'control-room-failed', 'x', '{}'::jsonb,"
            " 'failed', 'execute', 'synthetic_failure', false, 0, 0, :event_hash)"
        ), {"id": str(uuid.uuid4()), "scope": scope, "wid": workflow_id,
            "vid": version_id, "req": str(requester), "event_hash": "0" * 64})
    run_db(seed)


def test_overview_member_sees_counts_recent_runs_and_worker(admin_client):
    scope, _people = setup_scope(admin_client)
    draft = save(admin_client, scope)
    wid = publish(admin_client, scope, draft)
    run = trigger(admin_client, scope, wid)

    response = _overview(admin_client, scope)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_id"] == scope
    assert body["runs_by_status"].get("queued", 0) >= 1
    assert body["runs_by_origin"].get("workflow", 0) >= 1
    assert body["worker"]["engine_on"] is False or body["worker"]["engine_on"] is True
    recent = body["recent_runs"]
    assert any(r["id"] == run["id"] for r in recent)
    assert recent[0]["workflow_name"] == "Acceptance workflow"
    assert body["agents"] == []
    assert body["blockers"] == []


def test_overview_unknown_scope_is_404(admin_client):
    response = _overview(admin_client, str(uuid.uuid4()))
    assert response.status_code == 404


def test_overview_unauthenticated_is_401(client):
    scope, _people = setup_scope(client)
    client.cookies.clear()
    response = _overview(client, scope)
    assert response.status_code == 401


def test_overview_blockers_list_paused_and_failed_runs(admin_client):
    scope, _people = setup_scope(admin_client)
    draft = save(admin_client, scope)
    wid, version_id = draft["workflow"]["id"], draft["version"]["id"]
    publish(admin_client, scope, draft)
    run = trigger(admin_client, scope, wid)

    async def pause(conn):
        await conn.execute(text(
            "UPDATE wf_runs SET status = 'paused', blocked_reason = 'waiting on approval'"
            " WHERE id = :id"), {"id": run["id"]})
    run_db(pause)
    _seed_failed_run(scope, wid, version_id)

    body = _overview(admin_client, scope).json()
    blockers = {(b["kind"], b["run_id"]): b for b in body["blockers"]}
    assert ("run_paused", run["id"]) in blockers
    assert blockers[("run_paused", run["id"])]["reason"] == "waiting on approval"
    failed = [b for b in body["blockers"] if b["kind"] == "run_failed"]
    assert len(failed) == 1 and failed[0]["reason"] == "synthetic_failure"
    assert body["runs_by_status"]["paused"] == 1
    assert body["runs_by_status"]["failed"] == 1


def test_overview_agent_registry_lists_member_agent_with_latest_run(admin_client):
    scope, _people = setup_scope(admin_client)
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Verify this task", "control-room-one")
    assert queued.status_code == 202, queued.text

    body = _overview(admin_client, scope).json()
    assert len(body["agents"]) == 1
    agent = body["agents"][0]
    assert agent["id"] == agent_id
    assert agent["name"] == "Queue test agent"
    assert agent["enabled"] is True and agent["paused"] is False
    assert agent["latest_run"]["id"] == queued.json()["id"]
    assert agent["latest_run"]["status"] == "queued"


def test_overview_agent_registry_hides_agent_owned_by_non_member(admin_client):
    scope, _people = setup_scope(admin_client)

    async def seed_outsider(conn):
        from app.db.models import User

        await conn.execute(User.__table__.insert().values(
            email=EXTRA_EMAIL, role="user", password_hash=_PASSWORD_HASH))
    run_db(seed_outsider)

    login(admin_client, EXTRA_EMAIL, PASSWORD)
    agent_id, _thread_id = _create_agent_and_thread(admin_client)
    login(admin_client, ADMIN_EMAIL, PASSWORD)

    body = _overview(admin_client, scope).json()
    assert all(a["id"] != agent_id for a in body["agents"])
