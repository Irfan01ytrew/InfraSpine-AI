"""Scoped manager rights, one-time onboarding and durable employee handoffs."""

from sqlalchemy import text

from app.core.security import hash_session_token
from app.rag.answer import UNVERIFIED_FACT
from tests.conftest import ADMIN_EMAIL, CSRF, PASSWORD, USER2_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all


def _department(client, name="Finance"):
    response = client.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
    assert response.status_code == 201, response.text
    return response.json()


def _invite_manager(client, department_id):
    response = client.post(
        "/api/admin/users/invitations",
        json={"email": "manager@example.com", "role": "manager", "department_ids": [department_id]},
    )
    assert response.status_code == 201, response.text
    invitation = response.json()
    assert invitation["token"]
    stored_hash = run_db(
        lambda conn: conn.scalar(
            text("SELECT token_hash FROM user_invitations WHERE id = :id"), {"id": invitation["id"]}
        )
    )
    assert stored_hash == hash_session_token(invitation["token"])
    assert invitation["token"] not in stored_hash
    return invitation


def test_manager_invitation_is_single_use_and_document_access_is_department_scoped(client):
    login(client, ADMIN_EMAIL)
    department = _department(client)
    invitation = _invite_manager(client, department["id"])
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "manager-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "manager"
    reused = client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "another-password-123"},
        headers=CSRF,
    )
    assert reused.status_code == 403

    login(client, "manager@example.com", "manager-password-123")
    managed = client.get("/api/departments/managed").json()["groups"]
    assert [row["id"] for row in managed] == [department["id"]]
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204

    upload = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": department["id"]},
        files={"file": ("finance.pdf", make_pdf("Finance process details"), "application/pdf")},
    )
    assert upload.status_code == 202, upload.text
    document_id = upload.json()["id"]
    assert upload.json()["visibility"] == "restricted"

    # A department manager assignment does not expose the document to a different employee.
    login(client, USER2_EMAIL)
    visible = client.get("/api/rag/documents").json()["documents"]
    assert document_id in {row["id"] for row in visible}
    login(client, "user@example.com")
    visible = client.get("/api/rag/documents").json()["documents"]
    assert document_id not in {row["id"] for row in visible}


def test_manager_can_invite_only_an_employee_into_a_managed_department(client):
    login(client, ADMIN_EMAIL)
    managed = _department(client, "Managed invite scope")
    outside = _department(client, "Outside invite scope")
    manager_invite = _invite_manager(client, managed["id"])
    assert client.post(
        "/api/onboarding/accept",
        json={"token": manager_invite["token"], "password": "manager-password-123"},
        headers=CSRF,
    ).status_code == 200

    login(client, "manager@example.com", "manager-password-123")
    invited = client.post(
        f"/api/departments/{managed['id']}/invitations",
        json={"email": "scoped-new-hire@example.com"},
    )
    assert invited.status_code == 201, invited.text
    body = invited.json()
    assert body["role"] == "employee"
    assert body["department_ids"] == [managed["id"]]
    assert body["token"]
    listed = client.get(f"/api/departments/{managed['id']}/invitations")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["invitations"]) == 1
    assert listed.json()["invitations"][0]["token"] is None
    renewed = client.post(f"/api/departments/{managed['id']}/invitations/{body['id']}/resend")
    assert renewed.status_code == 200 and renewed.json()["token"] != body["token"]
    assert client.post(
        "/api/onboarding/accept",
        json={"token": body["token"], "password": "replay-password-123"},
        headers=CSRF,
    ).status_code == 403
    escaped = client.post(
        f"/api/departments/{outside['id']}/invitations",
        json={"email": "outside-new-hire@example.com"},
    )
    assert escaped.status_code == 404
    assert client.get(f"/api/departments/{outside['id']}/invitations").status_code == 404
    elevated = client.post(
        f"/api/departments/{managed['id']}/invitations",
        json={"email": "manager-upgrade@example.com", "role": "manager", "department_ids": [outside["id"]]},
    )
    assert elevated.status_code == 422

    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": renewed.json()["token"], "password": "employee-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200 and accepted.json()["role"] == "employee"
    login(client, "scoped-new-hire@example.com", "employee-password-123")
    assert client.get(f"/api/departments/{managed['id']}/invitations").status_code == 403
    assert client.post(
        f"/api/departments/{managed['id']}/invitations", json={"email": "self-elevation@example.com"}
    ).status_code == 403
    member_departments = client.get("/api/departments/mine").json()["groups"]
    assert [group["id"] for group in member_departments] == [managed["id"]]


def test_admin_resend_rotates_invitation_token_and_keeps_revocation_terminal(client):
    login(client, ADMIN_EMAIL)
    department = _department(client, "Invite recovery")
    original = client.post(
        "/api/admin/users/invitations",
        json={"email": "resend@example.com", "role": "employee", "department_ids": [department["id"]]},
    )
    assert original.status_code == 201, original.text
    original_body = original.json()
    renewed = client.post(f"/api/admin/users/invitations/{original_body['id']}/resend")
    assert renewed.status_code == 200, renewed.text
    renewed_body = renewed.json()
    assert renewed_body["token"] and renewed_body["token"] != original_body["token"]
    assert hash_session_token(renewed_body["token"]) != hash_session_token(original_body["token"])
    assert client.post(
        "/api/onboarding/accept",
        json={"token": original_body["token"], "password": "replay-password-123"},
        headers=CSRF,
    ).status_code == 403
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": renewed_body["token"], "password": "renewed-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200
    assert client.post(f"/api/admin/users/invitations/{original_body['id']}/resend").status_code == 409

    revoked = client.post(
        "/api/admin/users/invitations",
        json={"email": "revoked@example.com", "role": "employee", "department_ids": []},
    ).json()
    assert client.post(f"/api/admin/users/invitations/{revoked['id']}/revoke").status_code == 200
    assert client.post(f"/api/admin/users/invitations/{revoked['id']}/resend").status_code == 409


def test_admin_csv_invitation_preview_reports_rows_without_creating_invites(client):
    login(client, ADMIN_EMAIL)
    department = _department(client, "CSV preview department")
    pending = client.post(
        "/api/admin/users/invitations",
        json={"email": "pending@example.com", "role": "employee", "department_ids": [department["id"]]},
    )
    assert pending.status_code == 201
    before_count = run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM user_invitations")))
    csv_text = "\n".join([
        "email,role,departments",
        "good@example.com,employee,CSV preview department",
        "duplicate@example.com,employee,CSV preview department",
        "duplicate@example.com,employee,CSV preview department",
        "user@example.com,employee,",
        "pending@example.com,employee,CSV preview department",
        "unknown@example.com,employee,Missing department",
        "manager-empty@example.com,manager,",
        "manager-good@example.com,manager,CSV preview department",
    ])
    response = client.post("/api/admin/users/invitations/csv-preview", json={"csv_text": csv_text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invitations_sent"] is False
    assert (body["total_rows"], body["valid_rows"], body["invalid_rows"]) == (8, 3, 5)
    rows = {row["row_number"]: row for row in body["rows"]}
    assert rows[2]["valid"] and rows[2]["department_ids"] == [department["id"]]
    assert "duplicated in the CSV" in " ".join(rows[4]["errors"])
    assert "account already exists" in " ".join(rows[5]["errors"])
    assert "active invitation already exists" in " ".join(rows[6]["errors"])
    assert "does not exist" in " ".join(rows[7]["errors"])
    assert "at least one existing department" in " ".join(rows[8]["errors"])
    assert rows[9]["valid"]
    after_count = run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM user_invitations")))
    assert after_count == before_count
    audit_details = run_db(
        lambda conn: conn.scalar(
            text("SELECT details FROM audit_logs WHERE action='onboarding.csv_previewed' ORDER BY created_at DESC LIMIT 1")
        )
    )
    assert "good@example.com" not in str(audit_details)


def test_inaccessible_department_record_is_refused_without_general_answer(client, fake_provider):
    """A missing ACL-scoped hit must not turn an internal department-data question into general knowledge."""
    activate_provider(client)
    department = _department(client, "Finance")
    invitation = _invite_manager(client, department["id"])
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "manager-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200, accepted.text

    login(client, "manager@example.com", "manager-password-123")
    upload = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": department["id"]},
        files={
            "file": (
                "finance-close.pdf",
                make_pdf("Finance reconciliation target for batch F-42: USD 83,715."),
                "application/pdf",
            )
        },
    )
    assert upload.status_code == 202, upload.text
    document_id = upload.json()["id"]
    process_all(client)

    question = "What was the reconciliation target for Finance batch F-42?"
    fake_provider.rag_reply = lambda _message: "The target was USD 83,715. [S1]"
    authorized = client.post("/api/rag/chat", json={"question": question})
    assert authorized.status_code == 200, authorized.text
    assert "83,715" in authorized.json()["answer"]
    assert authorized.json()["citations"]

    fake_provider.calls.clear()
    login(client, USER2_EMAIL)
    denied = client.post("/api/rag/chat", json={"question": question})
    assert denied.status_code == 200, denied.text
    body = denied.json()
    assert body["answer"] == UNVERIFIED_FACT
    assert body["insufficient_evidence"] is True
    assert body["general_knowledge"] is False
    assert body["retrieved_chunks"] == 0 and body["citations"] == []
    assert body["strategy"] == "none"
    assert fake_provider.calls == []
    assert "83,715" not in body["answer"]

    login(client, "manager@example.com", "manager-password-123")
    user2_id = run_db(
        lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER2_EMAIL})
    )
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    login(client, USER2_EMAIL)
    newly_authorized = client.post("/api/rag/chat", json={"question": question})
    assert newly_authorized.status_code == 200 and "83,715" in newly_authorized.json()["answer"]
    assert newly_authorized.json()["citations"]

    login(client, "manager@example.com", "manager-password-123")
    assert client.delete(f"/api/departments/{department['id']}/members/{user2_id}").status_code == 204
    login(client, USER2_EMAIL)
    fake_provider.calls.clear()
    after_revocation = client.post("/api/rag/chat", json={"question": question})
    assert after_revocation.status_code == 200
    assert after_revocation.json()["answer"] == UNVERIFIED_FACT
    assert after_revocation.json()["insufficient_evidence"] is True
    assert after_revocation.json()["retrieved_chunks"] == 0 and after_revocation.json()["citations"] == []
    assert fake_provider.calls == []
    visible = client.get("/api/rag/documents").json()["documents"]
    assert document_id not in {row["id"] for row in visible}


def test_handoffs_survive_author_deactivation_and_require_department_manager_acknowledgement(client):
    login(client, ADMIN_EMAIL)
    department = _department(client, "Operations")
    invitation = _invite_manager(client, department["id"])
    assert client.post(
        "/api/onboarding/accept",
        json={"token": invitation["token"], "password": "manager-password-123"},
        headers=CSRF,
    ).status_code == 200
    login(client, "manager@example.com", "manager-password-123")
    assert client.post(
        f"/api/departments/{department['id']}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    login(client, USER2_EMAIL)
    submitted = client.post(
        "/api/handoffs",
        json={
            "department_id": department["id"],
            "title": "Weekly close checklist",
            "body": "1. Reconcile the pending invoices.\n2. Confirm the ledger export.",
        },
    )
    assert submitted.status_code == 201, submitted.text
    handoff_id = submitted.json()["id"]
    assert client.post(f"/api/handoffs/{handoff_id}/acknowledge").status_code == 404

    login(client, "manager@example.com", "manager-password-123")
    queue = client.get("/api/handoffs").json()["items"]
    assert any(item["id"] == handoff_id and item["body"].startswith("1. Reconcile") for item in queue)
    ack = client.post(f"/api/handoffs/{handoff_id}/acknowledge")
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged"

    login(client, ADMIN_EMAIL)
    users = client.get("/api/admin/users").json()["users"]
    employee = next(row for row in users if row["email"] == USER2_EMAIL)
    deactivated = client.post(f"/api/admin/users/{employee['id']}/active", json={"active": False})
    assert deactivated.status_code == 200 and deactivated.json()["is_active"] is False
    login(client, "manager@example.com", "manager-password-123")
    retained = client.get("/api/handoffs").json()["items"]
    assert any(item["id"] == handoff_id and item["status"] == "acknowledged" for item in retained)

