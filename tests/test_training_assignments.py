"""Scored employee quizzes and department assignments stay source and access scoped."""

import json

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all, ready
from tests.test_enterprise_people import _department, _invite_manager


def test_manager_assigns_cited_quiz_and_employee_score_is_private_and_append_only(client, fake_provider):
    login(client, ADMIN_EMAIL)
    department = _department(client, "Training Operations")
    invitation = _invite_manager(client, department["id"])
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "manager-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200
    activate_provider(client)
    source = ready(
        client,
        "training-vpn-policy.pdf",
        make_pdf("The VPN policy says rotate recovery tokens every 90 days and verify the recovery owner."),
        visibility="shared",
    )
    process_all(client)
    employee_id = next(
        row["id"] for row in client.get("/api/admin/users").json()["users"] if row["email"] == USER2_EMAIL
    )

    login(client, "manager@example.com", "manager-password-123")
    assert client.post(f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}).status_code == 204

    def answer(message: str) -> str:
        if "\nANSWER:\n" in message and "Passage 1 (" in message:
            return "SUPPORTED"
        if "Create a source-grounded multiple-choice quiz as JSON only." in message:
            questions = [
                {
                    "question": "Which documented check does the policy require before action?",
                    "choices": ["Verify the recovery owner", "Disable all access", "Skip review", "Delete the record"],
                    "correct_index": 0,
                    "explanation": "The policy requires verifying the recovery owner. [S1]",
                    "sources": ["S1"],
                }
                for _ in range(3)
            ]
            return json.dumps({"questions": questions})
        if "Privileged recovery owner" in message:
            return "Verify the privileged recovery owner [S1]."
        return "The policy requires rotating recovery tokens every 90 days [S1]."

    fake_provider.rag_reply = answer
    private_source = ready(
        client,
        "manager-private-training.pdf",
        make_pdf("For managers only, verify the privileged recovery owner."),
        visibility="private",
    )
    private_plan_response = client.post(
        "/api/rag/training",
        json={"topic": "Privileged recovery owner", "document_ids": [private_source["id"]]},
    )
    assert private_plan_response.status_code == 200, private_plan_response.text
    private_plan = private_plan_response.json()
    assert len(private_plan["quiz"]) == 3
    denied = client.post(
        f"/api/rag/training/{private_plan['id']}/assign",
        json={"department_id": department["id"], "user_ids": [employee_id]},
    )
    assert denied.status_code == 403

    generated = client.post(
        "/api/rag/training",
        json={"topic": "VPN recovery token process", "document_ids": [source["id"]]},
    )
    assert generated.status_code == 200, generated.text
    plan = generated.json()
    assert len(plan["quiz"]) == 3
    assert plan["quiz_required"] is True
    assert "correct_index" not in generated.text
    assert all(question["citations"] for question in plan["quiz"])

    assigned = client.post(
        f"/api/rag/training/{plan['id']}/assign",
        json={"department_id": department["id"], "user_ids": [employee_id], "due_at": "2030-01-31T23:59:59Z"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["created"] == 1
    assignment = assigned.json()["assignments"][0]
    assert assignment["assignee_email"] == USER2_EMAIL

    run_db(
        lambda conn: conn.execute(
            text("UPDATE training_assignments SET due_at = now() + interval '1 day' WHERE id = :id"),
            {"id": assignment["assignment_id"]},
        )
    )
    from app.main import app
    from app.services.training_reminders import process_training_reminders

    assert client.portal.call(process_training_reminders, app.state.sessionmaker) == 1
    assert client.portal.call(process_training_reminders, app.state.sessionmaker) == 0

    login(client, USER2_EMAIL)
    reminders = client.get("/api/rag/training/reminders")
    assert reminders.status_code == 200
    due_soon = reminders.json()["reminders"]
    assert len(due_soon) == 1 and due_soon[0]["reminder_type"] == "due_soon"
    read_reminder = client.post(f"/api/rag/training/reminders/{due_soon[0]['id']}/read")
    assert read_reminder.status_code == 200 and read_reminder.json()["read_at"]
    run_db(
        lambda conn: conn.execute(
            text("UPDATE training_assignments SET due_at = now() - interval '1 day' WHERE id = :id"),
            {"id": assignment["assignment_id"]},
        )
    )
    assert client.portal.call(process_training_reminders, app.state.sessionmaker) == 1
    reminders = client.get("/api/rag/training/reminders").json()["reminders"]
    assert {item["reminder_type"] for item in reminders} == {"due_soon", "overdue"}
    history = client.get("/api/rag/training").json()["items"]
    assert len(history) == 1 and history[0]["assigned"] is True
    assert history[0]["status"] == "in_progress"
    opened = client.get(f"/api/rag/training/{plan['id']}")
    assert opened.status_code == 200 and len(opened.json()["quiz"]) == 3
    assert opened.json()["status"] == "in_progress"
    assert "correct_index" not in opened.text
    submitted = client.post(
        f"/api/rag/training/{plan['id']}/quiz-attempts",
        json={"answers": [0, 0, 0]},
    )
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["score"] == 3 and result["max_score"] == 3 and result["passed"] is True
    assert all(row["correct"] for row in result["feedback"])
    attempts = client.get(f"/api/rag/training/{plan['id']}/quiz-attempts")
    assert attempts.status_code == 200
    assert attempts.json()["attempts"][0]["score"] == 3 and attempts.json()["attempts"][0]["passed"] is True
    with pytest.raises(Exception, match="training quiz attempts are append-only"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE training_quiz_attempts SET score = 0 WHERE id = :id"),
                {"id": result["attempt_id"]},
            )
        )
    completed = client.post(f"/api/rag/training/{plan['id']}/complete")
    assert completed.status_code == 200 and completed.json()["status"] == "completed"

    login(client, "manager@example.com", "manager-password-123")
    listing = client.get(f"/api/rag/training-assignments?department_id={department['id']}")
    assert listing.status_code == 200
    row = listing.json()["assignments"][0]
    assert row["latest_score"] == 3 and row["latest_max_score"] == 3 and row["status"] == "completed"
    assert client.get("/api/rag/training-assignment-candidates", params={"department_id": department["id"]}).json()["employees"] == [
        {"user_id": employee_id, "email": USER2_EMAIL}
    ]

    removed = client.delete(f"/api/departments/{department['id']}/members/{employee_id}")
    assert removed.status_code == 204
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/training/{plan['id']}").status_code == 404
    assert client.get("/api/rag/training").json()["items"] == []
    assert client.get("/api/rag/training/reminders").json()["reminders"] == []


def test_training_quiz_parser_fails_closed_without_matching_source_citations():
    from app.rag.training import parse_training_quiz

    assert parse_training_quiz('{"questions":[]}', [])[0] == []
    assert parse_training_quiz('{"questions":[{"question":"uncited"}]}', [])[1] == []
