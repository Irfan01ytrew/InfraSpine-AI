"""PostgreSQL acceptance for direct assignments, deadline fields and live department ACLs."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_enterprise_people import _department, _invite_manager


def _manager_with_employee(client):
    login(client, ADMIN_EMAIL)
    department = _department(client, "Personal Task Operations")
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
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": USER2_EMAIL})
    )
    return department, str(employee_id)


def _task_payload(title: str, *, assignee: str | None = None, department: str | None = None) -> dict:
    return {
        "title": title,
        "body": "Synthetic acceptance task. Review the assigned checklist and record the result.",
        "assignee_user_id": assignee,
        "department_group_id": department,
        "due_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }


def test_manager_assigns_employee_deadline_task_and_only_participants_can_read_or_complete(client):
    department, employee_id = _manager_with_employee(client)
    options = client.get("/api/personal-tasks/options")
    assert options.status_code == 200, options.text
    assert [item["id"] for item in options.json()["departments"]] == [department["id"]]
    assert options.json()["departments"][0]["members"] == [{"user_id": employee_id, "email": USER2_EMAIL}]

    due_at = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    payload = _task_payload("Review access register", assignee=employee_id, department=department["id"])
    payload["due_at"] = due_at.isoformat()
    created = client.post("/api/personal-tasks", json=payload)
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "open"
    assert task["assignee_user_id"] == employee_id
    assert task["created_by_email"] == "manager@example.com"
    assert task["assignee_email"] == USER2_EMAIL
    assert task["department_group_id"] == department["id"]
    assert datetime.fromisoformat(task["due_at"]) == due_at
    task_id = task["id"]

    # The creator and assignee can track the assignment; unrelated employees cannot enumerate it.
    assert [row["id"] for row in client.get("/api/personal-tasks").json()["tasks"]] == [task_id]
    assert client.get(f"/api/personal-tasks/{task_id}").status_code == 200
    login(client, USER2_EMAIL)
    assert [row["id"] for row in client.get("/api/personal-tasks").json()["tasks"]] == [task_id]
    assert client.post(f"/api/personal-tasks/{task_id}/complete").json()["status"] == "completed"
    assert client.post(f"/api/personal-tasks/{task_id}/complete").status_code == 409
    login(client, USER_EMAIL)
    assert client.get("/api/personal-tasks").json()["tasks"] == []
    assert client.get(f"/api/personal-tasks/{task_id}").status_code == 404
    assert client.post(f"/api/personal-tasks/{task_id}/complete").status_code == 404

    login(client, "manager@example.com", "manager-password-123")
    second = client.post(
        "/api/personal-tasks",
        json=_task_payload("Confirm quarterly review", assignee=employee_id, department=department["id"]),
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    assert client.delete(f"/api/departments/{department['id']}/members/{employee_id}").status_code == 204
    login(client, USER2_EMAIL)
    assert all(row["id"] != second_id for row in client.get("/api/personal-tasks").json()["tasks"])
    assert client.get(f"/api/personal-tasks/{second_id}").status_code == 404
    assert client.post(f"/api/personal-tasks/{second_id}/complete").status_code == 404


def test_employee_personal_task_cancel_and_assignment_boundaries_are_audited_privately(client):
    login(client, USER_EMAIL)
    task_payload = _task_payload("Prepare my handover checklist")
    created = client.post("/api/personal-tasks", json=task_payload)
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["assignee_user_id"] == task["created_by_user_id"]
    assert task["department_group_id"] is None
    assert client.post(f"/api/personal-tasks/{task['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/personal-tasks/{task['id']}/cancel").status_code == 409

    denied = client.post(
        "/api/personal-tasks",
        json=_task_payload(
            "Try to assign a coworker",
            assignee=str(run_db(
                lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": USER2_EMAIL})
            )),
        ),
    )
    assert denied.status_code == 403
    assert client.get("/api/personal-tasks/options").json()["departments"] == []
    login(client, USER2_EMAIL)
    assert client.get("/api/personal-tasks").json()["tasks"] == []
    assert client.get(f"/api/personal-tasks/{task['id']}").status_code == 404

    async def audit_rows(connection):
        result = await connection.execute(
            text("SELECT action, details FROM audit_logs WHERE action LIKE 'personal_task.%' ORDER BY action")
        )
        return result.all()

    rows = run_db(audit_rows)
    assert [row.action for row in rows] == ["personal_task.cancelled", "personal_task.created"]
    assert all("Prepare my handover checklist" not in str(row.details) for row in rows)
    assert all("Synthetic acceptance task" not in str(row.details) for row in rows)
