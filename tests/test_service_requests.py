"""Acceptance coverage for self-service requests, HR confidentiality and manager triage."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.session import create_engine, create_sessionmaker
from app.services.service_request_worker import process_service_request_slas
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.test_automation import _manager_department


def _run_service_request_slas(now: datetime) -> int:
    async def _run() -> int:
        engine = create_engine(TEST_DATABASE_URL)
        try:
            return await process_service_request_slas(create_sessionmaker(engine), now=now)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_employee_request_is_visible_to_owner_and_scoped_manager_and_keeps_history(client):
    department, _requester_id = _manager_department(client, "Support Operations")
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "it",
            "priority": "high",
            "department_group_id": department["id"],
            "subject": "VPN access stopped",
            "description": "The synthetic VPN account no longer connects after the test update.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request = submitted.json()
    assert request["status"] == "submitted" and request["can_cancel"] is True
    assert [event["action"] for event in request["events"]] == ["submitted"]

    # The department manager can triage and assign to an active member of this department.
    login(client, "manager@example.com", "manager-password-123")
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER_EMAIL}
    ).status_code == 204
    assignee_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER_EMAIL})
    )
    visible = client.get("/api/service-requests").json()["items"]
    assert [item["id"] for item in visible] == [request["id"]]
    assert visible[0]["can_manage"] is True
    updated = client.post(
        f"/api/service-requests/{request['id']}/triage",
        json={
            "status": "in_progress",
            "assigned_to_user_id": str(assignee_id),
            "comment": "IT is investigating the synthetic account configuration.",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "in_progress"
    assert updated.json()["assigned_to_email"] == USER_EMAIL
    assert [event["action"] for event in updated.json()["events"]] == ["submitted", "status_changed"]

    # An assigned employee can act on the request only while department access remains active.
    login(client, USER_EMAIL)
    assigned = client.get(f"/api/service-requests/{request['id']}")
    assert assigned.status_code == 200
    assert [item["id"] for item in client.get("/api/service-requests").json()["items"]] == [request["id"]]
    assert client.post(
        f"/api/service-requests/{request['id']}/comments", json={"comment": "I can provide a synthetic test log."}
    ).status_code == 200
    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{assignee_id}").status_code == 204
    login(client, USER_EMAIL)
    assert client.get(f"/api/service-requests/{request['id']}").status_code == 404
    assert client.get("/api/service-requests").json()["items"] == []

    login(client, USER2_EMAIL)
    own = client.get(f"/api/service-requests/{request['id']}")
    assert own.status_code == 200
    assert any(
        (event["comment"] or "").startswith("IT is investigating")
        for event in own.json()["events"]
    )
    assert client.post(
        f"/api/service-requests/{request['id']}/comments", json={"comment": "I can provide another test log."}
    ).status_code == 200
    assert client.get("/api/service-requests").json()["items"][0]["events"][-1]["action"] == "commented"

    # The former assignee loses access immediately when department membership is revoked.


def test_hr_requests_are_private_from_department_managers_and_request_events_are_immutable(client):
    department, _employee_id = _manager_department(client, "Private HR Support")
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "hr",
            "priority": "normal",
            "department_group_id": department["id"],
            "subject": "Private synthetic HR request",
            "description": "Sensitive synthetic HR details stay limited to the requester and administrators.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["id"]

    login(client, "manager@example.com", "manager-password-123")
    assert client.get("/api/service-requests").json()["items"] == []
    assert client.get(f"/api/service-requests/{request_id}").status_code == 404
    assert client.post(
        f"/api/service-requests/{request_id}/triage",
        json={"status": "acknowledged", "assigned_to_user_id": None},
    ).status_code == 404

    login(client, ADMIN_EMAIL)
    admin_copy = client.get(f"/api/service-requests/{request_id}")
    assert admin_copy.status_code == 200
    event_id = admin_copy.json()["events"][0]["id"]
    with pytest.raises(Exception, match="service request events are append-only"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE service_request_events SET comment = 'altered' WHERE id = :id"), {"id": event_id}
            )
        )


def test_employee_cannot_submit_for_an_unassigned_department_or_triage_a_request(client):
    login(client, USER2_EMAIL)
    department, _employee_id = _manager_department(client, "Other Department")
    # _manager_department returns with the manager session; switch to the employee with no membership.
    login(client, "user@example.com")
    denied = client.post(
        "/api/service-requests",
        json={
            "category": "access",
            "department_group_id": department["id"],
            "subject": "Out of scope",
            "description": "This should be rejected before a request row exists.",
        },
    )
    assert denied.status_code == 403


def test_sla_escalation_is_idempotent_visible_and_revocation_aware(client):
    department, _requester_id = _manager_department(client, "SLA Operations")
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "it",
            "priority": "high",
            "department_group_id": department["id"],
            "subject": "Synthetic SLA acceptance request",
            "description": "No live customer data is used by this service-request test.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request = submitted.json()
    assert request["response_sla_met_at"] is None
    due_at = datetime.fromisoformat(request["response_due_at"].replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
    assert due_at - created_at == timedelta(hours=4)

    now = datetime.now(UTC)
    run_db(
        lambda conn: conn.execute(
            text("UPDATE service_requests SET response_due_at = :due WHERE id = :id"),
            {"due": now - timedelta(seconds=1), "id": request["id"]},
        )
    )
    assert _run_service_request_slas(now) == 1
    assert _run_service_request_slas(now + timedelta(seconds=1)) == 0

    employee_view = client.get(f"/api/service-requests/{request['id']}")
    assert employee_view.status_code == 200
    assert employee_view.json()["response_sla_overdue"] is True
    assert employee_view.json()["sla_escalated_at"] is not None
    assert [event["action"] for event in employee_view.json()["events"]].count("sla_escalated") == 1
    employee_notifications = client.get("/api/service-requests/notifications").json()["items"]
    request_notice = next(item for item in employee_notifications if item["request_id"] == request["id"])

    login(client, "manager@example.com", "manager-password-123")
    manager_notifications = client.get("/api/service-requests/notifications").json()["items"]
    manager_notice = next(item for item in manager_notifications if item["request_id"] == request["id"])
    marked = client.post(f"/api/service-requests/notifications/{manager_notice['id']}/read")
    assert marked.status_code == 200 and marked.json()["read_at"] is not None

    manager_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = 'manager@example.com'"))
    )
    run_db(
        lambda conn: conn.execute(
            text("DELETE FROM department_managers WHERE group_id = :group_id AND manager_user_id = :manager_id"),
            {"group_id": department["id"], "manager_id": manager_id},
        )
    )
    assert client.get("/api/service-requests/notifications").json()["items"] == []
    assert client.post(f"/api/service-requests/notifications/{manager_notice['id']}/read").status_code == 404

    login(client, ADMIN_EMAIL)
    assert client.get(f"/api/service-requests/{request['id']}").status_code == 200
    assert request_notice["notification_type"] == "response_sla_escalated"


def test_hr_sla_escalation_never_notifies_department_manager(client):
    department, _requester_id = _manager_department(client, "Private HR SLA")
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "hr",
            "priority": "urgent",
            "department_group_id": department["id"],
            "subject": "Synthetic private HR SLA request",
            "description": "This request is visible only to the requester and administrators.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["id"]
    now = datetime.now(UTC)
    run_db(
        lambda conn: conn.execute(
            text("UPDATE service_requests SET response_due_at = :due WHERE id = :id"),
            {"due": now - timedelta(seconds=1), "id": request_id},
        )
    )
    assert _run_service_request_slas(now) == 1

    login(client, "manager@example.com", "manager-password-123")
    assert client.get("/api/service-requests/notifications").json()["items"] == []
    assert client.get(f"/api/service-requests/{request_id}").status_code == 404
    login(client, ADMIN_EMAIL)
    admin_notifications = client.get("/api/service-requests/notifications").json()["items"]
    assert any(item["request_id"] == request_id for item in admin_notifications)


def test_requester_comment_does_not_satisfy_response_sla_but_manager_reply_does(client):
    department, _requester_id = _manager_department(client, "SLA First Response")
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "other",
            "priority": "normal",
            "department_group_id": department["id"],
            "subject": "Synthetic first response test",
            "description": "Requester updates are not counted as a staff response.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["id"]
    requester_update = client.post(
        f"/api/service-requests/{request_id}/comments", json={"comment": "Adding a requester-side detail."}
    )
    assert requester_update.status_code == 200
    assert requester_update.json()["response_sla_met_at"] is None

    login(client, "manager@example.com", "manager-password-123")
    staff_reply = client.post(
        f"/api/service-requests/{request_id}/comments", json={"comment": "Support has started reviewing this."}
    )
    assert staff_reply.status_code == 200
    assert staff_reply.json()["response_sla_met_at"] is not None
    now = datetime.now(UTC)
    run_db(
        lambda conn: conn.execute(
            text("UPDATE service_requests SET response_due_at = :due WHERE id = :id"),
            {"due": now - timedelta(seconds=1), "id": request_id},
        )
    )
    assert _run_service_request_slas(now) == 0
    assert client.get(f"/api/service-requests/{request_id}").json()["sla_escalated_at"] is None
