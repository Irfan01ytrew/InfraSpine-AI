"""Synthetic PostgreSQL/API acceptance for resumable interviews and SOP review."""

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_automation import _manager_department


def test_interview_resume_template_snapshot_and_scoped_two_person_review(client):
    department, manager_employee_id = _manager_department(client, "Interview Process Operations")
    login(client, ADMIN_EMAIL)
    created_template = client.post(
        "/api/knowledge-interviews/templates",
        json={
            "slug": "monthly-close-interview",
            "title": "Monthly close handover",
            "description": "Capture repeatable monthly close work.",
            "questions": [
                {"key": "purpose", "prompt": "What is the purpose of this process?", "required": True},
                {"key": "steps", "prompt": "What steps should the next person follow?", "required": True},
            ],
        },
    )
    assert created_template.status_code == 201, created_template.text
    template_id = created_template.json()["id"]

    login(client, USER2_EMAIL)
    options = client.get("/api/knowledge-interviews/options")
    assert options.status_code == 200, options.text
    assert [group["id"] for group in options.json()["departments"]] == [department["id"]]
    started = client.post(
        "/api/knowledge-interviews/sessions",
        json={"template_id": template_id, "department_group_id": department["id"]},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    assert started.json()["can_edit"] is True and started.json()["sop_body"] == ""

    saved = client.patch(
        f"/api/knowledge-interviews/sessions/{session_id}",
        json={"answers": {"purpose": "Close the period and reconcile balances."}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["answers"]["purpose"] == "Close the period and reconcile balances."
    assert client.post(f"/api/knowledge-interviews/sessions/{session_id}/submit").status_code == 422

    # A template edit must not change an active employee session or its saved prompt set.
    login(client, ADMIN_EMAIL)
    updated = client.patch(
        f"/api/knowledge-interviews/templates/{template_id}",
        json={
            "is_active": False,
            "questions": [
                {"key": "purpose", "prompt": "Revised prompt for new interviews?", "required": True},
                {"key": "steps", "prompt": "Revised steps prompt?", "required": True},
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    login(client, USER2_EMAIL)
    snapshot = client.get(f"/api/knowledge-interviews/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["template_questions"][0]["prompt"] == "What is the purpose of this process?"

    outsider = client.get(f"/api/knowledge-interviews/sessions/{session_id}")
    assert outsider.status_code == 200  # USER2 owns the interview.
    login(client, USER_EMAIL)
    assert client.get(f"/api/knowledge-interviews/sessions/{session_id}").status_code == 404
    assert client.post(
        f"/api/knowledge-interviews/sessions/{session_id}/review",
        json={"decision": "approved", "rationale": "Not in this department."},
    ).status_code == 404

    login(client, USER2_EMAIL)
    completed = client.patch(
        f"/api/knowledge-interviews/sessions/{session_id}",
        json={"answers": {"steps": "Check totals, reconcile each account, and record the reviewer."}},
    )
    assert completed.status_code == 200, completed.text
    submitted = client.post(f"/api/knowledge-interviews/sessions/{session_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    assert submitted.json()["sop_title"] == "Monthly close handover"
    assert "reconcile each account" in submitted.json()["sop_body"]
    assert "No AI-generated claims or citations" in submitted.json()["sop_body"]
    assert len(submitted.json()["sop_revisions"]) == 1
    assert submitted.json()["sop_revisions"][0]["answers_snapshot"]["steps"].startswith("Check totals")

    self_review = client.post(
        f"/api/knowledge-interviews/sessions/{session_id}/review",
        json={"decision": "approved", "rationale": "Self review is disallowed."},
    )
    assert self_review.status_code == 403

    # The assigned department manager may request changes, then approve the next immutable revision.
    login(client, "manager@example.com", "manager-password-123")
    queue = client.get("/api/knowledge-interviews/sessions")
    assert queue.status_code == 200
    assert any(row["id"] == session_id and row["can_review"] for row in queue.json()["sessions"])
    requested = client.post(
        f"/api/knowledge-interviews/sessions/{session_id}/review",
        json={"decision": "changes_requested", "rationale": "Add the final verification owner."},
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "changes_requested"
    assert requested.json()["reviews"][0]["decision"] == "changes_requested"

    login(client, USER2_EMAIL)
    revised = client.patch(
        f"/api/knowledge-interviews/sessions/{session_id}",
        json={"answers": {"steps": "Check totals, reconcile each account, and record the reviewer and close date."}},
    )
    assert revised.status_code == 200, revised.text
    resubmitted = client.post(f"/api/knowledge-interviews/sessions/{session_id}/submit")
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["revision_number"] == 2
    assert len(resubmitted.json()["sop_revisions"]) == 2
    assert "reviewer and close date" not in resubmitted.json()["sop_revisions"][0]["body"]

    login(client, "manager@example.com", "manager-password-123")
    approved = client.post(
        f"/api/knowledge-interviews/sessions/{session_id}/review",
        json={"decision": "approved", "rationale": "The revised checks identify the close owner."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert len(approved.json()["reviews"]) == 2

    login(client, USER2_EMAIL)
    final = client.get(f"/api/knowledge-interviews/sessions/{session_id}")
    assert final.status_code == 200
    assert final.json()["can_edit"] is False
    assert final.json()["sop_body"].endswith("record the reviewer and close date.")
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM knowledge_interview_reviews"))) == 2
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM knowledge_interview_sop_revisions"))) == 2

    # Audit records never include employee responses or rendered SOP contents.
    async def interview_audit_rows(conn):
        result = await conn.execute(text(
            "SELECT details::text FROM audit_logs WHERE action LIKE 'knowledge.interview_%'"
        ))
        return result.scalars().all()

    audit_rows = run_db(interview_audit_rows)
    assert audit_rows
    assert all("reconcile each account" not in details for details in audit_rows)
    assert run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER2_EMAIL})) == manager_employee_id


def test_interview_templates_are_admin_managed_and_scope_is_checked(client):
    login(client, USER_EMAIL)
    assert client.post(
        "/api/knowledge-interviews/templates",
        json={"slug": "employee-template", "title": "Unauthorized", "description": "No", "questions": [
            {"key": "steps", "prompt": "What are the steps?", "required": True},
        ]},
    ).status_code == 403

    login(client, ADMIN_EMAIL)
    template_response = client.post(
        "/api/knowledge-interviews/templates",
        json={
            "slug": "interview-scope-boundary",
            "title": "Interview scope boundary",
            "description": "Synthetic template for department authorization checks.",
            "questions": [
                {"key": "steps", "prompt": "What are the steps?", "required": True},
            ],
        },
    )
    assert template_response.status_code == 201, template_response.text
    template = template_response.json()
    created = client.post(
        "/api/admin/access/groups",
        json={"name": "Interview Scope Boundary", "kind": "department"},
    )
    assert created.status_code == 201
    started = client.post(
        "/api/knowledge-interviews/sessions",
        json={"template_id": template["id"], "department_group_id": created.json()["id"]},
    )
    assert started.status_code == 201, started.text
    login(client, USER_EMAIL)
    denied = client.post(
        "/api/knowledge-interviews/sessions",
        json={"template_id": template["id"], "department_group_id": created.json()["id"]},
    )
    assert denied.status_code == 403
