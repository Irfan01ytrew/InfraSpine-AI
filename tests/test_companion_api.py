"""PostgreSQL acceptance for the Companion OS API (Step AN, slices 2–3).

Covers: goals/tasks/progress/briefing persistence, per-user isolation (404),
consent + approval-gated digest outbox (queue -> admin approve -> send),
revoke-cancels-pending, quiet-hours block, and per-user delete.

The digest "send" is recorded by monkeypatching
``app.companion.service.deliver_digest`` (and ``build_digest_message`` for the
sender, which is unconfigured in tests) — no SMTP is ever contacted.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db

ON = "2026-11-28"
OVERDUE = "2026-11-20"


def _goal(client, title="Learn Spanish", **kw):
    payload = {"title": title}
    payload.update(kw)
    created = client.post("/api/companion/goals", json=payload)
    assert created.status_code == 201, created.text
    return created.json()


def _task(client, goal_id, title="Do lesson 1", **kw):
    payload = {"goal_id": goal_id, "title": title}
    payload.update(kw)
    created = client.post("/api/companion/tasks", json=payload)
    assert created.status_code == 201, created.text
    return created.json()


def _consent(client):
    granted = client.post(
        "/api/companion/consents", json={"integration": "email_digest", "scope": "daily_digest"}
    )
    assert granted.status_code == 201, granted.text
    return granted.json()


def _queue(client):
    queued = client.post(f"/api/companion/digest/queue?on={ON}")
    assert queued.status_code == 201, queued.text
    return queued.json()


def _approve_as_admin(client, approval_id, action_hash):
    login(client, ADMIN_EMAIL)
    inbox = client.get("/api/approvals")
    assert inbox.status_code == 200, inbox.text
    assert any(row["id"] == approval_id for row in inbox.json()["approvals"])
    decided = client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "approve", "presented_hash": action_hash},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"


def test_goal_task_progress_briefing_flow(client):
    login(client, USER_EMAIL)
    project = client.post("/api/companion/projects", json={"name": "Growth"}).json()
    goal = _goal(client, project_id=project["id"])
    task = _task(client, goal["id"], due=OVERDUE, estimated_hours=2.0)
    progress = client.post(
        "/api/companion/progress",
        json={"goal_id": goal["id"], "task_id": task["id"], "percent": 50, "note": "half done", "on": "2026-11-27"},
    )
    assert progress.status_code == 201, progress.text
    assert progress.json()["percent"] == 50

    briefing = client.get(f"/api/companion/briefing?on={ON}")
    assert briefing.status_code == 200, briefing.text
    body = briefing.json()
    assert body["plan"]["overdue_count"] == 1
    assert body["plan"]["tasks"][0]["task_id"] == task["id"]
    assert body["plan"]["total_estimated_hours"] == 2.0
    assert body["goals"][0]["percent"] == 50
    assert body["goals"][0]["tasks_total"] == 1
    assert body["stalled_goal_ids"] == []  # progress recorded yesterday

    plan = client.get(f"/api/companion/plan?on={ON}")
    assert plan.status_code == 200, plan.text
    assert plan.json()["overdue_count"] == 1


def test_preferences_defaults_and_update(client):
    login(client, USER_EMAIL)
    prefs = client.get("/api/companion/preferences").json()
    assert prefs == {
        "language": "en", "timezone": "Asia/Karachi", "weekly_capacity_hours": 40.0,
        "quiet_start": "22:00:00", "quiet_end": "07:00:00", "retention_days": 90,
    }
    updated = client.put(
        "/api/companion/preferences", json={"language": "roman_urdu", "retention_days": 30}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["language"] == "roman_urdu"
    assert updated.json()["retention_days"] == 30
    assert updated.json()["timezone"] == "Asia/Karachi"  # untouched fields keep their values


def test_other_users_rows_are_404_and_briefing_is_own_data_only(client):
    login(client, USER_EMAIL)
    goal = _goal(client)
    _task(client, goal["id"])

    login(client, USER2_EMAIL)
    assert client.get("/api/companion/goals").json() == []
    assert client.get(f"/api/companion/goals/{goal['id']}").status_code == 404
    assert client.get("/api/companion/tasks").json() == []
    briefing = client.get(f"/api/companion/briefing?on={ON}")
    assert briefing.status_code == 200, briefing.text
    assert briefing.json()["plan"]["tasks"] == []
    assert briefing.json()["goals"] == []


def test_progress_rules_reject_completion_without_evidence_and_backwards(client):
    login(client, USER_EMAIL)
    goal = _goal(client)
    task = _task(client, goal["id"])
    base = {"goal_id": goal["id"], "task_id": task["id"], "on": "2026-11-27"}
    assert client.post("/api/companion/progress", json={**base, "percent": 100, "note": ""}).status_code == 422
    assert client.post("/api/companion/progress", json={**base, "percent": 101, "note": "x"}).status_code == 422
    ok = client.post("/api/companion/progress", json={**base, "percent": 60, "note": "more than half"})
    assert ok.status_code == 201, ok.text
    assert client.post("/api/companion/progress", json={**base, "percent": 40, "note": "less"}).status_code == 422
    other_goal = _goal(client, "Other goal")
    assert client.post(
        "/api/companion/progress",
        json={"goal_id": other_goal["id"], "task_id": task["id"], "percent": 10, "note": "x", "on": "2026-11-27"},
    ).status_code == 422


def test_digest_queue_requires_consent(client):
    login(client, USER_EMAIL)
    _goal(client)
    queued = client.post(f"/api/companion/digest/queue?on={ON}")
    assert queued.status_code == 409
    assert "consent" in queued.json()["error"]["message"].lower()


def test_digest_full_flow_queue_approve_send(client, monkeypatch):
    sent = []

    def fake_deliver(settings, message):
        sent.append(message)

    monkeypatch.setattr("app.companion.service.deliver_digest", fake_deliver)
    # The sender address is unconfigured in tests; supply a test sender.
    monkeypatch.setattr(
        "app.companion.service.digest_sender", lambda settings: "companion@example.com"
    )

    login(client, USER_EMAIL)
    goal = _goal(client)
    _task(client, goal["id"], due=OVERDUE)
    _consent(client)
    # Keep the send outside quiet hours whatever time the suite runs: a
    # one-hour quiet window starting two hours from now (UTC).
    now_utc = datetime.now(timezone.utc)
    start = (now_utc + timedelta(hours=2)).time().replace(microsecond=0)
    end = (now_utc + timedelta(hours=3)).time().replace(microsecond=0)
    updated = client.put(
        "/api/companion/preferences",
        json={"timezone": "UTC", "quiet_start": start.isoformat(), "quiet_end": end.isoformat()},
    )
    assert updated.status_code == 200, updated.text
    queued = _queue(client)
    digest_id = queued["id"]

    # The requester cannot approve their own digest (separation of duties).
    own = client.post(
        f"/api/approvals/{queued['approval_id']}/decide",
        json={"decision": "approve", "presented_hash": queued["action_hash"]},
    )
    assert own.status_code == 403, own.text

    # Sending before approval is refused: the approval is still pending.
    early = client.post(f"/api/companion/digest/{digest_id}/send", json={"approval_id": queued["approval_id"]})
    assert early.status_code == 409, early.text

    _approve_as_admin(client, queued["approval_id"], queued["action_hash"])

    login(client, USER_EMAIL)
    sent_response = client.post(
        f"/api/companion/digest/{digest_id}/send", json={"approval_id": queued["approval_id"]}
    )
    assert sent_response.status_code == 200, sent_response.text
    assert sent_response.json()["status"] == "sent"
    assert sent_response.json()["message_id"]
    assert len(sent) == 1
    assert sent[0]["To"] == USER_EMAIL
    assert "Learn Spanish" in sent[0].get_content()

    # The approval is single-use: a second send is refused.
    again = client.post(
        f"/api/companion/digest/{digest_id}/send", json={"approval_id": queued["approval_id"]}
    )
    assert again.status_code == 409, again.text

    digests = client.get("/api/companion/digest").json()
    assert [row["status"] for row in digests] == ["sent"]


def test_digest_body_contains_only_the_owners_data(client):
    login(client, USER2_EMAIL)
    _goal(client, "User2 secret goal")
    login(client, USER_EMAIL)
    _goal(client, "My own goal")
    _consent(client)
    queued = _queue(client)
    body = run_db(
        lambda conn: conn.scalar(
            text("SELECT body FROM companion_digest_outbox WHERE id=:id"), {"id": queued["id"]}
        )
    )
    assert "My own goal" in body
    assert "User2 secret goal" not in body


def test_revoke_consent_cancels_pending_digest_and_blocks_send(client):
    login(client, USER_EMAIL)
    _goal(client)
    consent = _consent(client)
    queued = _queue(client)
    revoked = client.post(f"/api/companion/consents/{consent['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active"] is False
    digests = client.get("/api/companion/digest").json()
    assert digests[0]["status"] == "cancelled"
    send = client.post(
        f"/api/companion/digest/{queued['id']}/send", json={"approval_id": queued["approval_id"]}
    )
    assert send.status_code == 409, send.text


def test_send_during_quiet_hours_is_refused(client, monkeypatch):
    monkeypatch.setattr("app.companion.service.deliver_digest", lambda settings, message: None)
    login(client, USER_EMAIL)
    _goal(client)
    _consent(client)
    # Quiet window covering right now (UTC), so the send must refuse whatever the clock says.
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(hours=1)).time().replace(microsecond=0)
    end = (now_utc + timedelta(hours=1)).time().replace(microsecond=0)
    updated = client.put(
        "/api/companion/preferences",
        json={"timezone": "UTC", "quiet_start": start.isoformat(), "quiet_end": end.isoformat()},
    )
    assert updated.status_code == 200, updated.text
    queued = _queue(client)
    _approve_as_admin(client, queued["approval_id"], queued["action_hash"])
    login(client, USER_EMAIL)
    send = client.post(
        f"/api/companion/digest/{queued['id']}/send", json={"approval_id": queued["approval_id"]}
    )
    assert send.status_code == 409, send.text
    assert "quiet hours" in send.json()["error"]["message"].lower()


def test_delete_all_erases_only_the_requesters_data(client):
    login(client, USER_EMAIL)
    _goal(client, "Mine")
    login(client, USER2_EMAIL)
    other_goal = _goal(client, "Theirs")
    login(client, USER_EMAIL)
    assert client.delete("/api/companion/data").status_code == 422  # confirm required
    deleted = client.delete("/api/companion/data?confirm=true")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["goals"] == 1
    assert client.get("/api/companion/goals").json() == []
    login(client, USER2_EMAIL)
    assert [row["id"] for row in client.get("/api/companion/goals").json()] == [other_goal["id"]]
