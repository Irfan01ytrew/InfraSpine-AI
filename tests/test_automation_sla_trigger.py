"""Approval-gated workflows for department response-SLA escalation."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.main import app
from app.services.automation import enqueue_service_request_workflows
from app.services.automation_worker import process_next_workflow_run
from app.services.service_request_worker import process_service_request_slas
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, login, run_db
from tests.test_automation import _create_workflow, _manager_department


def test_sla_event_workflow_is_scoped_idempotent_and_approval_gated(client):
    department, employee_id = _manager_department(client, "SLA Workflow Operations")
    workflow = _create_workflow(client, department["id"], employee_id, trigger_type="request_sla")
    preview = client.post(f"/api/automation/workflows/{workflow['id']}/dry-run")
    assert preview.status_code == 200
    assert preview.json()["nodes"][0]["title"] == "Department help request response target missed"
    assert preview.json()["side_effects_performed"] is False
    login(client, USER2_EMAIL)
    requests = []
    for category, group, subject in (("it", department["id"], "Scoped synthetic SLA"),
                                     ("hr", department["id"], "Private synthetic HR"),
                                     ("other", None, "Unscoped synthetic SLA")):
        response = client.post("/api/service-requests", json={"category": category, "priority": "high",
            "department_group_id": group, "subject": subject, "description": "Synthetic event test content."})
        assert response.status_code == 201, response.text
        requests.append(response.json())
    assert client.get("/api/automation/runs").json()["runs"] == []
    assert client.get("/api/automation/tasks").json()["tasks"] == []

    now = datetime.now(UTC)
    async def make_due(conn):
        for item in requests:
            await conn.execute(text("UPDATE service_requests SET response_due_at=:due WHERE id=:id"),
                               {"due": now - timedelta(seconds=1), "id": uuid.UUID(item["id"])})
    run_db(make_due)
    async def process_slas():
        return await process_service_request_slas(app.state.sessionmaker, now=now)
    assert client.portal.call(process_slas) == 3
    assert client.portal.call(process_slas) == 0
    runs = client.get("/api/automation/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["workflow_id"] == workflow["id"]
    run = runs[0]
    assert run["status"] == "pending_approval" and run["requested_by_user_id"] == str(employee_id)
    assert client.post(f"/api/automation/runs/{run['id']}/decision",
                       json={"approved": True, "rationale": "Requester cannot approve own run."}).status_code == 403
    assert client.get("/api/automation/tasks").json()["tasks"] == []

    async def replay(request_id):
        async with app.state.sessionmaker() as db:
            return await enqueue_service_request_workflows(db, request_id=uuid.UUID(request_id),
                department_group_id=uuid.UUID(department["id"]), requester_user_id=employee_id,
                event_type="sla_escalated", now=now)
    assert client.portal.call(replay, requests[0]["id"]) == 0
    assert client.portal.call(replay, requests[1]["id"]) == 0  # HR cannot be forged into a workflow.

    login(client, "manager@example.com", "manager-password-123")
    notices = client.get("/api/automation/notifications").json()["items"]
    assert any(item["run_id"] == run["id"] and item["notification_type"] == "approval_required" for item in notices)
    approved = client.post(f"/api/automation/runs/{run['id']}/decision",
        json={"approved": True, "rationale": "Independent manager reviewed the missed response target."})
    assert approved.status_code == 200 and approved.json()["status"] == "queued"
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker) is True
    login(client, USER2_EMAIL)
    tasks = client.get("/api/automation/tasks").json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["title"] == "Review access membership"
    assert client.get("/api/automation/runs").json()["runs"][0]["status"] == "completed"
    async def audit_rows(conn):
        return (await conn.execute(text("SELECT details::text FROM audit_logs WHERE action='automation.event_run_created'"))).scalars().all()
    audit = run_db(audit_rows)
    assert len(audit) == 1 and "service_request_sla_escalated" in audit[0]
    assert "Private synthetic HR" not in audit[0] and "Synthetic event test content" not in audit[0]
