"""Synthetic acceptance coverage for approval-gated department workflows."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.test_enterprise_people import _department, _invite_manager


def _manager_department(client, name="Workflow Operations"):
    login(client, ADMIN_EMAIL)
    department = _department(client, name)
    invitation = _invite_manager(client, department["id"])
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "manager-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200, accepted.text
    login(client, "manager@example.com", "manager-password-123")
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    employee_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER2_EMAIL})
    )
    return department, employee_id


def _create_workflow(client, department_id, employee_id, *, trigger_type="manual", interval_minutes=None):
    response = client.post(
        "/api/automation/workflows",
        json={
            "name": "Quarterly access review",
            "department_group_id": department_id,
            "trigger_type": trigger_type,
            "interval_minutes": interval_minutes,
            "assignee_user_id": str(employee_id),
            "task_title": "Review access membership",
            "task_body": "Check active members against the current approved access register.",
            "due_days": 5,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_workflow_requires_second_person_approval_and_recovers_after_membership_change(client):
    department, employee_id = _manager_department(client)
    workflow = _create_workflow(client, department["id"], employee_id)

    login(client, USER2_EMAIL)
    key = str(uuid.uuid4())
    requested = client.post(
        f"/api/automation/workflows/{workflow['id']}/runs", json={"idempotency_key": key}
    )
    assert requested.status_code == 202, requested.text
    run = requested.json()
    assert run["status"] == "pending_approval" and run["can_cancel"] is True
    repeated = client.post(
        f"/api/automation/workflows/{workflow['id']}/runs", json={"idempotency_key": key}
    )
    assert repeated.status_code == 202 and repeated.json()["id"] == run["id"]
    assert client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "Self approval should be denied."},
    ).status_code == 403

    login(client, "manager@example.com", "manager-password-123")
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "The department owner reviewed this task request."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"
    assert approved.json()["approval_rationale"] == "The department owner reviewed this task request."

    # Membership is checked at execution as well as at definition time.
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    from app.main import app
    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    failed_once = client.get("/api/automation/runs").json()["runs"][0]
    assert failed_once["id"] == run["id"]
    assert failed_once["status"] == "queued" and failed_once["attempts"] == 1
    assert failed_once["last_error_code"] == "assignee_membership_revoked"
    assert client.get("/api/automation/tasks").json()["tasks"] == []
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    run_db(
        lambda conn: conn.execute(
            text("UPDATE automation_runs SET available_at = now() WHERE id = :id"), {"id": run["id"]}
        )
    )
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    completed = client.get("/api/automation/runs").json()["runs"][0]
    assert completed["status"] == "completed" and completed["attempts"] == 2
    own_tasks = client.get("/api/automation/tasks").json()["tasks"]
    assert len(own_tasks) == 1 and own_tasks[0]["status"] == "open"
    assert own_tasks[0]["title"] == "Review access membership"
    with pytest.raises(Exception, match="workflow approvals are append-only"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE automation_approvals SET rationale = 'Changed after decision' WHERE run_id = :id"),
                {"id": run["id"]},
            )
        )
    with pytest.raises(Exception, match="workflow task content is immutable"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE automation_tasks SET title = 'Tampered task' WHERE run_id = :id"),
                {"id": run["id"]},
            )
        )

    login(client, "user@example.com")
    assert client.get("/api/automation/tasks").json()["tasks"] == []
    assert client.post(f"/api/automation/tasks/{own_tasks[0]['id']}/complete").status_code == 403

    login(client, USER2_EMAIL)
    assert client.post(f"/api/automation/tasks/{own_tasks[0]['id']}/complete").status_code == 200
    assert client.get("/api/automation/tasks").json()["tasks"][0]["status"] == "completed"
    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert client.get("/api/automation/tasks").json()["tasks"] == []


def test_reclaimed_workflow_rejects_stale_execution_and_failure(client, monkeypatch):
    from app.main import app
    from app.services import automation_worker
    from app.services.automation_worker import _claim_next, _execute, _record_failure

    department, employee_id = _manager_department(client)
    workflow = _create_workflow(client, department["id"], employee_id)
    login(client, USER2_EMAIL)
    created = client.post(
        f"/api/automation/workflows/{workflow['id']}/runs",
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    login(client, "manager@example.com", "manager-password-123")
    assert client.post(
        f"/api/automation/runs/{run_id}/decision",
        json={"approved": True, "rationale": "Approve recovery test."},
    ).status_code == 200

    async def reclaim():
        sessions = app.state.sessionmaker
        first = await _claim_next(sessions, datetime.now(UTC))
        assert first is not None and str(first.run_id) == run_id
        assert await _claim_next(sessions, first.expires_at - timedelta(seconds=1)) is None
        reclaimed_at = first.expires_at + timedelta(seconds=1)
        # The old claim is still in the row, but its lease has expired by the time
        # the failure recorder inspects the locked row.
        with monkeypatch.context() as clock:
            class ExpiredLeaseClock:
                @staticmethod
                def now(_tz):
                    return reclaimed_at
            clock.setattr(automation_worker, "datetime", ExpiredLeaseClock)
            await _record_failure(sessions, first, "expired_worker")
        second = await _claim_next(sessions, reclaimed_at)
        assert second is not None and second.run_id == first.run_id and second.attempt == 2
        await _execute(sessions, first)
        await _record_failure(sessions, first, "stale_worker")
        return second

    second = client.portal.call(reclaim)
    run = client.get("/api/automation/runs").json()["runs"][0]
    assert run["status"] == "processing" and run["attempts"] == 2
    assert run["last_error_code"] is None
    assert client.get("/api/automation/tasks").json()["tasks"] == []
    client.portal.call(_execute, app.state.sessionmaker, second)
    client.portal.call(_execute, app.state.sessionmaker, second)
    assert client.get("/api/automation/runs").json()["runs"][0]["status"] == "completed"
    assert len(client.get("/api/automation/tasks").json()["tasks"]) == 1


def test_approved_member_message_is_scoped_revocable_and_retry_safe(client):
    department, employee_id = _manager_department(client, "Workflow Messaging")
    other_user_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER_EMAIL})
    )
    login(client, "manager@example.com", "manager-password-123")
    invalid_scope = client.post(
        "/api/automation/workflows",
        json={
            "name": "Out-of-scope message",
            "department_group_id": department["id"],
            "action_type": "notify_member",
            "notification_recipient_user_id": str(other_user_id),
            "notification_title": "Private reminder",
            "notification_body": "This user is not in the department.",
        },
    )
    assert invalid_scope.status_code == 422

    created = client.post(
        "/api/automation/workflows",
        json={
            "name": "Quarterly member reminder",
            "department_group_id": department["id"],
            "trigger_type": "manual",
            "action_type": "notify_member",
            "notification_recipient_user_id": str(employee_id),
            "notification_title": "Quarterly review",
            "notification_body": "Please complete your access review.",
        },
    )
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["action_type"] == "notify_member"
    assert workflow["notification_recipient_user_id"] == str(employee_id)
    assert workflow["task_title"] is None and workflow["task_body"] is None

    preview = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run")
    assert preview.status_code == 200
    assert preview.json()["task_preview"] is None
    assert preview.json()["notification_preview"]["recipient_email"] == USER2_EMAIL

    login(client, USER2_EMAIL)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={}).json()
    assert run["status"] == "pending_approval"
    denied = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "A requester cannot approve their own message."},
    )
    assert denied.status_code == 403

    login(client, "manager@example.com", "manager-password-123")
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "The department manager reviewed this member message."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204

    from app.main import app
    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    failed = client.get("/api/automation/runs").json()["runs"][0]
    assert failed["status"] == "queued" and failed["attempts"] == 1
    assert failed["last_error_code"] == "recipient_membership_revoked"
    assert run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM workflow_notifications WHERE run_id = :id AND notification_type = 'workflow_message'"),
            {"id": run["id"]},
        )
    ) == 0

    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    run_db(
        lambda conn: conn.execute(
            text("UPDATE automation_runs SET available_at = now() WHERE id = :id"), {"id": run["id"]}
        )
    )
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    completed = client.get("/api/automation/runs").json()["runs"][0]
    assert completed["status"] == "completed" and completed["attempts"] == 2

    login(client, USER2_EMAIL)
    messages = [item for item in client.get("/api/automation/notifications").json()["items"] if item["notification_type"] == "workflow_message"]
    assert len(messages) == 1
    message = messages[0]
    assert message["task_id"] is None
    assert message["message_title"] == "Quarterly review"
    assert message["message_body"] == "Please complete your access review."
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is False
    assert run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM workflow_notifications WHERE run_id = :id AND notification_type = 'workflow_message'"),
            {"id": run["id"]},
        )
    ) == 1

    login(client, "manager@example.com", "manager-password-123")
    assert all(item["notification_type"] != "workflow_message" for item in client.get("/api/automation/notifications").json()["items"])
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert all(item["notification_type"] != "workflow_message" for item in client.get("/api/automation/notifications").json()["items"])
    assert client.post(f"/api/automation/notifications/{message['id']}/read").status_code == 404

def test_workflow_versions_are_immutable_and_interval_scheduler_is_idempotent(client):
    department, employee_id = _manager_department(client, "Workflow Scheduling")
    workflow = _create_workflow(
        client, department["id"], employee_id, trigger_type="interval", interval_minutes=60
    )
    updated = client.put(
        f"/api/automation/workflows/{workflow['id']}",
        json={
            "expected_version": 1,
            "name": "Quarterly access review v2",
            "trigger_type": "interval",
            "interval_minutes": 120,
            "assignee_user_id": str(employee_id),
            "task_title": "Review privileged access",
            "task_body": "Verify each privileged account against the approved roster.",
            "due_days": 10,
        },
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["version"] == 2
    stale = client.put(
        f"/api/automation/workflows/{workflow['id']}",
        json={
            "expected_version": 1,
            "name": "Stale edit",
            "trigger_type": "interval",
            "interval_minutes": 120,
            "assignee_user_id": str(employee_id),
            "task_title": "Review privileged access",
            "task_body": "Verify each privileged account against the approved roster.",
            "due_days": None,
        },
    )
    assert stale.status_code == 409
    visible = client.get("/api/automation/workflows").json()["workflows"]
    assert [row["version"] for row in visible] == [2]

    run_db(
        lambda conn: conn.execute(
            text("UPDATE automation_workflows SET next_run_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": updated.json()["id"]},
        )
    )
    from app.main import app
    from app.services.automation_worker import schedule_due_workflows

    assert client.portal.call(schedule_due_workflows, app.state.sessionmaker) == 1
    assert client.portal.call(schedule_due_workflows, app.state.sessionmaker) == 0
    scheduled = client.get("/api/automation/runs").json()["runs"]
    assert len(scheduled) == 1 and scheduled[0]["status"] == "pending_approval"
    login(client, ADMIN_EMAIL)
    scheduled_notices = client.get("/api/automation/notifications").json()["items"]
    assert any(
        item["run_id"] == scheduled[0]["id"] and item["notification_type"] == "approval_required"
        for item in scheduled_notices
    )


def test_service_request_event_trigger_is_idempotent_approval_gated_and_hr_private(client):
    department, employee_id = _manager_department(client, "Event Trigger Operations")
    workflow = _create_workflow(
        client, department["id"], employee_id, trigger_type="service_request", interval_minutes=None
    )
    assert workflow["trigger_type"] == "service_request"
    preview = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run")
    assert preview.status_code == 200, preview.text
    assert preview.json()["nodes"][0]["title"] == "Department help request submitted"
    assert preview.json()["side_effects_performed"] is False

    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/service-requests",
        json={
            "category": "it", "priority": "high", "department_group_id": department["id"],
            "subject": "Synthetic access request", "description": "Please review the synthetic access issue.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["id"]
    runs = client.get("/api/automation/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "pending_approval"
    run = runs[0]
    assert run["requested_by_user_id"] == str(employee_id)
    assert run["can_approve"] is False
    assert client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "The requester cannot approve this event run."},
    ).status_code == 403

    # A duplicate delivery for the same request/workflow is ignored by the run idempotency key.
    from app.main import app
    from app.services.automation import enqueue_service_request_workflows

    async def _enqueue_duplicate_event():
        async with app.state.sessionmaker() as db:
            return await enqueue_service_request_workflows(
                db,
                request_id=uuid.UUID(request_id),
                department_group_id=uuid.UUID(department["id"]),
                requester_user_id=employee_id,
            )

    assert client.portal.call(_enqueue_duplicate_event) == 0

    # HR requests and unscoped requests never create department workflow runs.
    hr_request = client.post(
        "/api/service-requests",
        json={
            "category": "hr", "priority": "normal", "department_group_id": department["id"],
            "subject": "Private synthetic HR request", "description": "Private content must not trigger a department workflow.",
        },
    )
    unscoped_request = client.post(
        "/api/service-requests",
        json={
            "category": "other", "priority": "normal", "department_group_id": None,
            "subject": "Unscoped synthetic request", "description": "No department automation should run.",
        },
    )
    assert hr_request.status_code == unscoped_request.status_code == 201
    assert len(client.get("/api/automation/runs").json()["runs"]) == 1

    login(client, "manager@example.com", "manager-password-123")
    notifications = client.get("/api/automation/notifications").json()["items"]
    assert any(item["run_id"] == run["id"] and item["notification_type"] == "approval_required" for item in notifications)
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "The department manager independently reviewed the event."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"

    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    login(client, USER2_EMAIL)
    tasks = client.get("/api/automation/tasks").json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["title"] == "Review access membership"
    persisted = client.get("/api/automation/runs").json()["runs"]
    assert len(persisted) == 1 and persisted[0]["status"] == "completed"
    async def _event_run_audit(conn):
        result = await conn.execute(
            text("SELECT details::text FROM audit_logs WHERE action='automation.event_run_created'")
        )
        return result.scalars().all()

    audit_details = run_db(_event_run_audit)
    assert len(audit_details) == 1
    assert all("Private synthetic HR request" not in row and "synthetic access issue" not in row for row in audit_details)


def test_department_manager_cannot_approve_own_run_and_can_cancel_it(client):
    department, employee_id = _manager_department(client, "Workflow Separation of Duties")
    workflow = _create_workflow(client, department["id"], employee_id)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={}).json()
    denied = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "The requester is also attempting approval."},
    )
    assert denied.status_code == 403
    cancelled = client.post(f"/api/automation/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_workflow_definition_rejects_out_of_department_assignee_and_employee_cannot_configure(client):
    department, _employee_id = _manager_department(client, "Workflow Authorization")
    login(client, "manager@example.com", "manager-password-123")
    other_employee_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = 'user@example.com'"))
    )
    denied_assignee = client.post(
        "/api/automation/workflows",
        json={
            "name": "Invalid assignee",
            "department_group_id": department["id"],
            "trigger_type": "manual",
            "interval_minutes": None,
            "assignee_user_id": str(other_employee_id),
            "task_title": "Check access",
            "task_body": "Review the active access list carefully.",
            "due_days": None,
        },
    )
    assert denied_assignee.status_code == 422

    login(client, USER2_EMAIL)
    assert client.post(
        "/api/automation/workflows",
        json={
            "name": "Employee-created workflow",
            "department_group_id": department["id"],
            "trigger_type": "manual",
            "interval_minutes": None,
            "assignee_user_id": str(other_employee_id),
            "task_title": "Check access",
            "task_body": "Review the active access list carefully.",
            "due_days": None,
        },
    ).status_code == 403


def test_workflow_dry_run_is_manager_only_and_does_not_create_history_or_tasks(client):
    department, employee_id = _manager_department(client, "Workflow Dry Run")
    workflow = _create_workflow(client, department["id"], employee_id)

    # Keep all count reads on one test connection and prove dry-run has no writes.
    async def read_counts(conn):
        counts = []
        for table in ("automation_runs", "automation_approvals", "automation_tasks", "audit_logs"):
            counts.append(await conn.scalar(text(f"SELECT count(*) FROM {table}")))
        return tuple(counts)

    login(client, ADMIN_EMAIL)
    before = run_db(read_counts)
    admin_preview = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run", json={})
    assert admin_preview.status_code == 200, admin_preview.text
    assert admin_preview.json()["side_effects_performed"] is False
    assert run_db(read_counts) == before

    login(client, "manager@example.com", "manager-password-123")
    before = run_db(read_counts)
    response = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run", json={})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["valid"] is True and plan["side_effects_performed"] is False
    assert plan["workflow_version"] == 1
    assert [node["id"] for node in plan["nodes"]] == ["trigger", "approval", "task", "completion"]
    assert plan["edges"] == [
        {"source": "trigger", "target": "approval"},
        {"source": "approval", "target": "task"},
        {"source": "task", "target": "completion"},
    ]
    assert all(check["passed"] for check in plan["checks"])
    assert plan["task_preview"]["title"] == "Review access membership"
    assert plan["task_preview"]["assignee_email"] == USER2_EMAIL
    assert run_db(read_counts) == before

    login(client, USER2_EMAIL)
    before = run_db(read_counts)
    forbidden = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run", json={})
    assert forbidden.status_code == 403
    assert run_db(read_counts) == before

    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    before = run_db(read_counts)
    invalid = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run", json={})
    assert invalid.status_code == 422
    assert run_db(read_counts) == before


def test_approved_personal_task_action_is_myinbox_scoped_and_idempotent(client):
    department, employee_id = _manager_department(client, "Workflow My Work")
    workflow_response = client.post(
        "/api/automation/workflows",
        json={
            "name": "Assign follow-up in My Work",
            "department_group_id": department["id"],
            "trigger_type": "manual",
            "interval_minutes": None,
            "action_type": "assign_personal_task",
            "assignee_user_id": str(employee_id),
            "task_title": "Review the approved access list",
            "task_body": "Compare active access to the current synthetic register.",
            "due_days": 3,
        },
    )
    assert workflow_response.status_code == 201, workflow_response.text
    workflow = workflow_response.json()
    assert workflow["action_type"] == "assign_personal_task"
    assert workflow["task_title"] == "Review the approved access list"

    preview = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run")
    assert preview.status_code == 200, preview.text
    assert preview.json()["side_effects_performed"] is False
    assert preview.json()["nodes"][2]["title"] == "Create one My Work assignment"
    assert preview.json()["task_preview"]["assignee_email"] == USER2_EMAIL

    login(client, USER2_EMAIL)
    run_response = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={})
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    assert run["status"] == "pending_approval"
    assert client.get("/api/personal-tasks").json()["tasks"] == []

    login(client, "manager@example.com", "manager-password-123")
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "Approved the department follow-up assignment."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"

    from app.main import app
    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    completed = client.get("/api/automation/runs").json()["runs"]
    assert len(completed) == 1 and completed[0]["status"] == "completed"
    tasks = client.get("/api/personal-tasks").json()["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["created_by_email"] == "manager@example.com"
    assert task["assignee_email"] == USER2_EMAIL
    assert task["department_group_id"] == department["id"]
    assert task["title"] == "Review the approved access list"
    assert task["due_at"] is not None
    assert run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM personal_tasks WHERE automation_run_id = :id"),
            {"id": run["id"]},
        )
    ) == 1
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is False

    login(client, USER2_EMAIL)
    visible_to_assignee = client.get("/api/personal-tasks").json()["tasks"]
    assert [item["id"] for item in visible_to_assignee] == [task["id"]]
    assert client.post(f"/api/personal-tasks/{task['id']}/complete").json()["status"] == "completed"

    login(client, USER_EMAIL)
    assert client.get("/api/personal-tasks").json()["tasks"] == []
    assert client.get(f"/api/personal-tasks/{task['id']}").status_code == 404

    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert all(row["id"] != task["id"] for row in client.get("/api/personal-tasks").json()["tasks"])
    assert client.get(f"/api/personal-tasks/{task['id']}").status_code == 404


def test_workflow_notifications_follow_approval_task_and_current_membership(client):
    department, employee_id = _manager_department(client, "Workflow Notification Lifecycle")
    workflow = _create_workflow(client, department["id"], employee_id)

    login(client, USER2_EMAIL)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={}).json()
    assert client.get("/api/automation/notifications").json()["items"] == []

    login(client, "manager@example.com", "manager-password-123")
    reviewer_items = client.get("/api/automation/notifications").json()["items"]
    review_notice = next(item for item in reviewer_items if item["run_id"] == run["id"])
    assert review_notice["notification_type"] == "approval_required"
    assert review_notice["task_id"] is None and review_notice["read_at"] is None
    read_review = client.post(f"/api/automation/notifications/{review_notice['id']}/read")
    assert read_review.status_code == 200 and read_review.json()["read_at"] is not None
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "Reviewed for the synthetic test department."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"
    manager_after_approval = client.get("/api/automation/notifications").json()["items"]
    assert len(manager_after_approval) == 1
    assert manager_after_approval[0]["id"] == review_notice["id"]
    assert manager_after_approval[0]["read_at"] == read_review.json()["read_at"]

    login(client, USER2_EMAIL)
    approval_items = client.get("/api/automation/notifications").json()["items"]
    assert len(approval_items) == 1
    approval_notification = approval_items[0]
    assert approval_notification["notification_type"] == "run_approved"
    assert approval_notification["run_id"] == run["id"]
    assert approval_notification["task_id"] is None
    assert approval_notification["read_at"] is None

    login(client, "manager@example.com", "manager-password-123")
    assert client.post(
        f"/api/automation/notifications/{approval_notification['id']}/read"
    ).status_code == 404

    from app.main import app
    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    login(client, USER2_EMAIL)
    notifications = client.get("/api/automation/notifications").json()["items"]
    by_type = {item["notification_type"]: item for item in notifications}
    assert set(by_type) == {"run_approved", "task_assigned"}
    task = client.get("/api/automation/tasks").json()["tasks"][0]
    assert by_type["task_assigned"]["task_id"] == task["id"]
    assert by_type["task_assigned"]["run_id"] == run["id"]
    assert by_type["task_assigned"]["read_at"] is None

    read = client.post(f"/api/automation/notifications/{approval_notification['id']}/read")
    assert read.status_code == 200 and read.json()["read_at"] is not None
    assert client.post(f"/api/automation/notifications/{approval_notification['id']}/read").json()["read_at"] == read.json()["read_at"]

    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert client.get("/api/automation/notifications").json()["items"] == []
    assert client.post(
        f"/api/automation/notifications/{by_type['task_assigned']['id']}/read"
    ).status_code == 404


def test_rejected_workflow_sends_only_requester_notification(client):
    department, employee_id = _manager_department(client, "Workflow Rejection Notice")
    workflow = _create_workflow(client, department["id"], employee_id)
    login(client, USER2_EMAIL)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={}).json()

    login(client, "manager@example.com", "manager-password-123")
    rejected = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": False, "rationale": "The synthetic request needs revision first."},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"

    login(client, USER2_EMAIL)
    notifications = client.get("/api/automation/notifications").json()["items"]
    assert len(notifications) == 1
    assert notifications[0]["notification_type"] == "run_rejected"
    assert notifications[0]["run_id"] == run["id"]
    assert client.get("/api/automation/tasks").json()["tasks"] == []


@pytest.mark.skipif(
    os.environ.get("RUN_C15_RECOVERY_INSTALL") != "1",
    reason="requires an explicitly configured disposable PostgreSQL recovery target",
)
def test_approved_workflow_recovers_after_database_clone(client):
    """Exercise lease recovery from a restored synthetic database snapshot.

    This opt-in acceptance is deliberately pinned to the retained loopback QA cluster and
    uniquely named *_test databases. It never connects to the application database.
    """
    import asyncio
    import json
    import os
    import re
    import subprocess
    from pathlib import Path

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services.automation_worker import process_next_workflow_run
    from tests.conftest import TEST_DATABASE_URL

    source_url = make_url(TEST_DATABASE_URL)
    assert source_url.host in {"127.0.0.1", "localhost", "::1"}
    assert source_url.port == 55928
    assert source_url.database and source_url.database.endswith("_test")

    target_name = os.environ.get("C15_RECOVERY_DATABASE_NAME", "")
    assert re.fullmatch(r"infraspine_c15recovered_[0-9]{8}(?:_r[0-9]+)?_test", target_name)
    evidence_dir = Path(os.environ.get("C15_RECOVERY_EVIDENCE_DIR", "")).resolve()
    project_root = Path.cwd().resolve()
    assert evidence_dir != project_root and project_root in evidence_dir.parents
    evidence_dir.mkdir(parents=True, exist_ok=True)
    archive = evidence_dir / "approved-workflow-recovery.dump"
    assert not archive.exists(), "refusing to overwrite a prior recovery artifact"

    department, employee_id = _manager_department(client, "C15 Recovered Workflow")
    workflow = _create_workflow(client, department["id"], employee_id)
    login(client, USER2_EMAIL)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs", json={}).json()
    login(client, "manager@example.com", "manager-password-123")
    approved = client.post(
        f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "Synthetic recovery acceptance approval."},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "queued"

    # The claim commits before execution. Model a worker that was interrupted after its
    # claim and then let the lease expire before taking the database snapshot.
    run_db(
        lambda conn: conn.execute(
            text(
                "UPDATE automation_runs SET status = 'processing', attempts = 1, "
                "locked_until = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": run["id"]},
        )
    )
    source_head = run_db(
        lambda conn: conn.scalar(text("SELECT version_num FROM alembic_version"))
    )

    postgres_tools = Path(r"C:\Program Files\PostgreSQL")
    tool_dirs = sorted(postgres_tools.glob("*/bin"), reverse=True)
    pg_dump = next((path / "pg_dump.exe" for path in tool_dirs if (path / "pg_dump.exe").is_file()), None)
    pg_restore = next((path / "pg_restore.exe" for path in tool_dirs if (path / "pg_restore.exe").is_file()), None)
    assert pg_dump is not None and pg_restore is not None

    async def create_recovery_database() -> None:
        admin_url = source_url.set(database="postgres")
        engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_name}
                )
                assert exists is None, "refusing to overwrite an existing recovery database"
                await connection.execute(text(f'CREATE DATABASE "{target_name}"'))
        finally:
            await engine.dispose()

    client.portal.call(create_recovery_database)
    cli_source_url = source_url.set(drivername="postgresql", password=None)
    cli_target_url = source_url.set(database=target_name, drivername="postgresql", password=None)
    pg_env = os.environ.copy()
    if source_url.password:
        pg_env["PGPASSWORD"] = source_url.password
    dump_result = subprocess.run(
        [str(pg_dump), "--no-password", "--format=custom", "--file", str(archive),
         "--dbname", cli_source_url.render_as_string(hide_password=False)],
        capture_output=True, text=True, timeout=180, env=pg_env, check=False,
    )
    assert dump_result.returncode == 0, f"pg_dump failed with exit code {dump_result.returncode}"
    restore_result = subprocess.run(
        [str(pg_restore), "--no-password", "--exit-on-error", "--no-owner", "--no-privileges",
         "--dbname", cli_target_url.render_as_string(hide_password=False), str(archive)],
        capture_output=True, text=True, timeout=180, env=pg_env, check=False,
    )
    assert restore_result.returncode == 0, f"pg_restore failed with exit code {restore_result.returncode}"

    async def recover_and_read() -> dict:
        recovery_url = source_url.set(database=target_name)
        engine = create_async_engine(recovery_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            worked = await process_next_workflow_run(factory)
            worked_again = await process_next_workflow_run(factory)
            async with engine.connect() as connection:
                state = (
                    await connection.execute(
                        text(
                            "SELECT status, attempts, last_error_code FROM automation_runs WHERE id = :id"
                        ),
                        {"id": run["id"]},
                    )
                ).one()
                tasks = await connection.scalar(
                    text("SELECT count(*) FROM automation_tasks WHERE run_id = :id"),
                    {"id": run["id"]},
                )
                notices = await connection.scalar(
                    text(
                        "SELECT count(*) FROM workflow_notifications WHERE run_id = :id "
                        "AND notification_type = 'task_assigned'"
                    ),
                    {"id": run["id"]},
                )
                alembic_head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            return {
                "worked": worked,
                "worked_again": worked_again,
                "status": state.status,
                "attempts": state.attempts,
                "last_error_code": state.last_error_code,
                "tasks": tasks,
                "task_assigned_notices": notices,
                "migration": alembic_head,
            }
        finally:
            await engine.dispose()

    recovered = client.portal.call(recover_and_read)
    assert recovered == {
        "worked": True,
        "worked_again": False,
        "status": "completed",
        "attempts": 2,
        "last_error_code": None,
        "tasks": 1,
        "task_assigned_notices": 1,
        "migration": source_head,
    }

    (evidence_dir / "recovered-workflow-acceptance.json").write_text(
        json.dumps(
            {
                "source_database": source_url.database,
                "recovered_database": target_name,
                "migration": recovered["migration"],
                "run_id": run["id"],
                "result": recovered,
                "archive_bytes": archive.stat().st_size,
                "recovered_after_process_interruption": True,
                "duplicate_execution_prevented": (
                    not recovered["worked_again"]
                    and recovered["tasks"] == recovered["task_assigned_notices"] == 1
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
