"""PostgreSQL acceptance for scheduled personal-task reminders and live ACL checks."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import USER2_EMAIL, login, run_db
from tests.test_personal_tasks import _manager_with_employee, _task_payload


def test_personal_task_due_soon_overdue_and_read_receipts_are_idempotent_and_private(client):
    department, employee_id = _manager_with_employee(client)
    payload = _task_payload("Review expiring equipment checks", assignee=employee_id, department=department["id"])
    payload["due_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    created = client.post("/api/personal-tasks", json=payload)
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    from app.main import app
    from app.services.personal_task_reminders import process_personal_task_reminders

    assert client.portal.call(process_personal_task_reminders, app.state.sessionmaker) == 1
    assert client.portal.call(process_personal_task_reminders, app.state.sessionmaker) == 0
    assert client.get("/api/personal-tasks/reminders").json()["reminders"] == []

    login(client, USER2_EMAIL)
    reminders = client.get("/api/personal-tasks/reminders")
    assert reminders.status_code == 200
    due_soon = reminders.json()["reminders"]
    assert len(due_soon) == 1 and due_soon[0]["reminder_type"] == "due_soon"
    assert due_soon[0]["task_id"] == task_id
    read = client.post(f"/api/personal-tasks/reminders/{due_soon[0]['id']}/read")
    assert read.status_code == 200 and read.json()["read_at"]
    read_at = read.json()["read_at"]
    assert client.post(f"/api/personal-tasks/reminders/{due_soon[0]['id']}/read").json()["read_at"] == read_at

    run_db(
        lambda connection: connection.execute(
            text("UPDATE personal_tasks SET due_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": task_id},
        )
    )
    assert client.portal.call(process_personal_task_reminders, app.state.sessionmaker) == 1
    reminders = client.get("/api/personal-tasks/reminders").json()["reminders"]
    assert {item["reminder_type"] for item in reminders} == {"due_soon", "overdue"}
    assert client.post(f"/api/personal-tasks/{task_id}/complete").status_code == 200
    assert client.get("/api/personal-tasks/reminders").json()["reminders"] == []


def test_personal_task_reminder_is_hidden_and_unreadable_after_department_revocation(client):
    department, employee_id = _manager_with_employee(client)
    payload = _task_payload("Verify revoked access is applied", assignee=employee_id, department=department["id"])
    payload["due_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    created = client.post("/api/personal-tasks", json=payload)
    assert created.status_code == 201, created.text

    from app.main import app
    from app.services.personal_task_reminders import process_personal_task_reminders

    assert client.portal.call(process_personal_task_reminders, app.state.sessionmaker) == 1
    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert client.get("/api/personal-tasks/reminders").json()["reminders"] == []
    reminder_id = run_db(
        lambda connection: connection.scalar(
            text("SELECT id FROM personal_task_reminders WHERE task_id=:task_id"),
            {"task_id": created.json()["id"]},
        )
    )
    assert client.post(f"/api/personal-tasks/reminders/{reminder_id}/read").status_code == 404


def test_personal_task_email_outbox_retries_and_exposes_only_recipient_delivery_status(
    client, monkeypatch
):
    department, employee_id = _manager_with_employee(client)
    due_at = datetime.now(timezone.utc) + timedelta(days=2)
    payload = {
        "title": "Synthetic deadline email acceptance",
        "body": "This synthetic task text must never be included in the email.",
        "assignee_user_id": employee_id,
        "department_group_id": department["id"],
        "due_at": due_at.isoformat(),
    }
    created = client.post("/api/personal-tasks", json=payload)
    assert created.status_code == 201, created.text

    from app.core.config import get_settings
    from app.main import app
    from app.services import personal_task_reminders

    settings = get_settings()
    monkeypatch.setattr(settings, "personal_task_email_enabled", True)
    attempts: list[tuple[str, str]] = []

    def fail_once(current_settings, recipient: str, reminder_id):
        attempts.append((recipient, str(reminder_id)))
        if len(attempts) == 1:
            raise OSError("synthetic relay interruption")

    monkeypatch.setattr(personal_task_reminders, "send_personal_task_reminder_email", fail_once)
    first_now = datetime.now(timezone.utc)

    async def process_at(now):
        return await personal_task_reminders.process_personal_task_reminders(
            app.state.sessionmaker, settings=settings, now=now
        )

    assert client.portal.call(process_at, first_now) == 1
    reminder_id = attempts[0][1]

    async def email_state(connection):
        return (
            await connection.execute(
                text("SELECT email_status, email_attempts, email_last_error_code FROM personal_task_reminders WHERE id=:id"),
                {"id": reminder_id},
            )
        ).one()

    status = run_db(email_state)
    assert tuple(status) == ("retry", 1, "OSError")

    assert client.portal.call(process_at, first_now + timedelta(minutes=1)) == 0
    assert attempts == [(USER2_EMAIL, reminder_id), (USER2_EMAIL, reminder_id)]
    persisted = run_db(email_state)
    assert tuple(persisted) == ("sent", 2, None)

    login(client, USER2_EMAIL)
    reminders = client.get("/api/personal-tasks/reminders").json()["reminders"]
    assert len(reminders) == 1 and reminders[0]["email_status"] == "sent"
    assert reminders[0]["task_id"] == created.json()["id"]


