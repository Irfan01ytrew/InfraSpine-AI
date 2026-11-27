"""Custom role templates materialize only existing department-scoped grants and revoke atomically."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db


def _expiry(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _user_id(email: str) -> str:
    return str(run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": email})))


def _department(client, name: str) -> str:
    response = client.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _claim(client, scope: str, statement: str):
    response = client.post(
        "/api/claims",
        json={
            "kind": "proposal",
            "statement": statement,
            "category": "financial",
            "scope_group_id": scope,
            "sources": [{"kind": "note", "note": "Synthetic QA evidence note."}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_custom_role_template_enforces_base_role_scope_and_immediate_revocation(client):
    login(client, ADMIN_EMAIL)
    finance = _department(client, "Finance Template Scope")
    other = _department(client, "Other Template Scope")

    users = client.get("/api/admin/users").json()["users"]
    user_ids = {row["email"]: row["id"] for row in users}
    assert client.patch(
        f"/api/admin/users/{user_ids[USER_EMAIL]}",
        json={"role": "employee", "department_ids": [finance, other]},
    ).status_code == 200
    assert client.patch(
        f"/api/admin/users/{user_ids[USER2_EMAIL]}",
        json={"role": "employee", "department_ids": [finance]},
    ).status_code == 200

    invalid_admin_role = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Forbidden admin role",
            "base_role": "admin",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert invalid_admin_role.status_code == 422
    steward_template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Scoped stewardship role",
            "base_role": "employee",
            "permissions": [{"permission": "data.steward", "categories": ["financial"]}],
        },
    )
    assert steward_template.status_code == 201, steward_template.text
    catalog = client.get("/api/admin/access/permission-catalog")
    assert catalog.status_code == 200
    assert {(row["resource"], row["action"], tuple(row["scope_dimensions"])) for row in catalog.json()["permissions"]} == {
        ("knowledge", "review", ("category", "access_group")),
        ("knowledge", "publish", ("category", "access_group")),
        ("data", "steward", ("category", "access_group")),
        ("reports", "view", ()),  # organization-wide, read-only (see test_reports_permission.py)
        ("people", "invite", ("access_group",)),  # department-scoped (see test_department_capabilities.py)
        ("training", "manage", ("access_group",)),
        ("agents", "manage", ("access_group",)),
    }

    created = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Finance reviewer",
            "description": "Review submitted Finance claims.",
            "base_role": "employee",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert created.status_code == 201, created.text
    template_id = created.json()["id"]
    assert created.json()["version"] == 1 and created.json()["is_active"] is True

    missing_expiry = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance]},
    )
    assert missing_expiry.status_code == 422
    for invalid_expiry in (
        datetime.now(UTC).isoformat(),
        (datetime.now(UTC) + timedelta(days=366)).isoformat(),
        (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat(),
    ):
        invalid_assignment = client.post(
            f"/api/admin/role-templates/{template_id}/assignments",
            json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": invalid_expiry},
        )
        assert invalid_assignment.status_code == 422, invalid_assignment.text

    manager_template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Department manager reviewer",
            "base_role": "manager",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert manager_template.status_code == 201, manager_template.text
    wrong_base = client.post(
        f"/api/admin/role-templates/{manager_template.json()['id']}/assignments",
        json={"email": USER_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert wrong_base.status_code == 422
    self_grant = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": ADMIN_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert self_grant.status_code == 403
    project = client.post("/api/admin/access/groups", json={"name": "Out of scope project", "kind": "project"})
    assert project.status_code == 201
    wrong_scope = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [project.json()["id"]], "expires_at": _expiry()},
    )
    assert wrong_scope.status_code == 422
    outside_membership = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [other], "expires_at": _expiry()},
    )
    assert outside_membership.status_code == 422

    assignment = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert assignment.status_code == 201, assignment.text
    assert assignment.json()["expires_at"] is not None
    assignment_id = assignment.json()["id"]
    assert assignment.json()["grant_count"] == 1
    steward_assignment = client.post(
        f"/api/admin/role-templates/{steward_template.json()['id']}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert steward_assignment.status_code == 201, steward_assignment.text
    assert steward_assignment.json()["grant_count"] == 1
    assert run_db(lambda conn: conn.scalar(
        text(
            "SELECT permission FROM permission_grants WHERE role_template_assignment_id = CAST(:id AS uuid)"
        ),
        {"id": steward_assignment.json()["id"]},
    )) == "data.steward"

    login(client, USER_EMAIL)
    finance_claim = _claim(client, finance, "Synthetic Finance approval request one.")
    other_claim = _claim(client, other, "Synthetic Finance approval request two.")

    login(client, USER2_EMAIL)
    allowed = client.post(
        f"/api/governance/claims/{finance_claim['id']}/decisions",
        json={"stage": "review", "rationale": "Reviewed within the assigned Finance department."},
    )
    assert allowed.status_code == 200, allowed.text
    denied_publish = client.post(
        f"/api/governance/claims/{other_claim['id']}/decisions",
        json={"stage": "review", "rationale": "This claim is outside the assigned department."},
    )
    assert denied_publish.status_code == 403
    wrong_stage = client.post(
        f"/api/governance/claims/{finance_claim['id']}/decisions",
        json={"stage": "publish", "rationale": "Review-only role cannot publish."},
    )
    assert wrong_stage.status_code == 403

    login(client, ADMIN_EMAIL)
    listed = client.get("/api/admin/role-template-assignments")
    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()["assignments"]} == {
        assignment_id,
        steward_assignment.json()["id"],
    }
    revoked = client.post(f"/api/admin/role-template-assignments/{assignment_id}/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    assert revoked.json()["grant_count"] == 1

    login(client, USER2_EMAIL)
    after_revoke = _claim(client, finance, "Synthetic Finance approval request after revocation.")
    denied_after_revoke = client.post(
        f"/api/governance/claims/{after_revoke['id']}/decisions",
        json={"stage": "review", "rationale": "Revoked Finance reviewer cannot decide."},
    )
    assert denied_after_revoke.status_code == 403

    login(client, ADMIN_EMAIL)
    assert client.patch(f"/api/admin/role-templates/{template_id}/active", json={"active": False}).status_code == 200
    templates = client.get("/api/admin/role-templates").json()["templates"]
    assert next(row for row in templates if row["id"] == template_id)["is_active"] is False
    with pytest.raises(Exception, match="role template definition is immutable"):
        run_db(lambda conn: conn.execute(
            text("UPDATE role_templates SET permission_specs = '[]'::jsonb WHERE id = CAST(:id AS uuid)"),
            {"id": template_id},
        ))


def test_department_change_and_offboarding_revoke_template_grants(client):
    login(client, ADMIN_EMAIL)
    finance = _department(client, "Finance revocation scope")
    users = {row["email"]: row["id"] for row in client.get("/api/admin/users").json()["users"]}
    assert client.patch(
        f"/api/admin/users/{users[USER_EMAIL]}",
        json={"role": "employee", "department_ids": [finance]},
    ).status_code == 200
    assert client.patch(
        f"/api/admin/users/{users[USER2_EMAIL]}",
        json={"role": "employee", "department_ids": [finance]},
    ).status_code == 200
    template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Finance approver lifecycle",
            "base_role": "employee",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert template.status_code == 201, template.text

    def assign():
        response = client.post(
            f"/api/admin/role-templates/{template.json()['id']}/assignments",
            json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    department_assignment_id = assign()
    removed = client.delete(f"/api/admin/access/groups/{finance}/members/{users[USER2_EMAIL]}")
    assert removed.status_code == 204, removed.text
    assert run_db(lambda conn: conn.scalar(
        text("SELECT revoked_at IS NOT NULL FROM role_template_assignments WHERE id = CAST(:id AS uuid)"),
        {"id": department_assignment_id},
    )) is True
    assert client.post(
        f"/api/admin/access/groups/{finance}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    changed_assignment_id = assign()
    # The separate role-management path must also revoke the linked grant in the same transaction.
    changed = client.patch(
        f"/api/admin/users/{users[USER2_EMAIL]}",
        json={"role": "employee", "department_ids": []},
    )
    assert changed.status_code == 200, changed.text
    assert run_db(lambda conn: conn.scalar(
        text("SELECT revoked_at IS NOT NULL FROM role_template_assignments WHERE id = CAST(:id AS uuid)"),
        {"id": changed_assignment_id},
    )) is True
    assert client.patch(
        f"/api/admin/users/{users[USER2_EMAIL]}",
        json={"role": "employee", "department_ids": [finance]},
    ).status_code == 200
    login(client, USER_EMAIL)
    claim = _claim(client, finance, "Synthetic Finance decision after department change.")
    login(client, USER2_EMAIL)
    assert client.post(
        f"/api/governance/claims/{claim['id']}/decisions",
        json={"stage": "review", "rationale": "Department-change revocation must be immediate."},
    ).status_code == 403

    login(client, ADMIN_EMAIL)
    offboarding_assignment_id = assign()
    deactivated = client.post(
        f"/api/admin/users/{users[USER2_EMAIL]}/active",
        json={"active": False, "knowledge_transfer_to": users[ADMIN_EMAIL]},
    )
    assert deactivated.status_code == 200, deactivated.text
    async def _offboarding_rows(conn):
        result = await conn.execute(
            text(
                "SELECT a.revoked_at IS NOT NULL, g.revoked_at IS NOT NULL "
                "FROM role_template_assignments a JOIN permission_grants g ON g.role_template_assignment_id=a.id "
                "WHERE a.id = CAST(:id AS uuid)"
            ),
            {"id": offboarding_assignment_id},
        )
        return result.one()

    assignment_rows = run_db(_offboarding_rows)
    assert tuple(assignment_rows) == (True, True)


def test_role_template_grant_stops_authorizing_at_assignment_expiry(client):
    login(client, ADMIN_EMAIL)
    finance = _department(client, "Finance expiring role scope")
    users = {row["email"]: row["id"] for row in client.get("/api/admin/users").json()["users"]}
    for email in (USER_EMAIL, USER2_EMAIL):
        response = client.patch(
            f"/api/admin/users/{users[email]}",
            json={"role": "employee", "department_ids": [finance]},
        )
        assert response.status_code == 200, response.text

    template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Short-lived Finance reviewer",
            "base_role": "employee",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert template.status_code == 201, template.text
    expiry = datetime.now(UTC) + timedelta(seconds=10)
    assignment = client.post(
        f"/api/admin/role-templates/{template.json()['id']}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": expiry.isoformat()},
    )
    assert assignment.status_code == 201, assignment.text
    assert datetime.fromisoformat(assignment.json()["expires_at"]) == expiry

    login(client, USER_EMAIL)
    before_claim = _claim(client, finance, "Synthetic claim reviewed before role expiry.")
    after_claim = _claim(client, finance, "Synthetic claim denied after role expiry.")
    login(client, USER2_EMAIL)
    before_expiry = client.post(
        f"/api/governance/claims/{before_claim['id']}/decisions",
        json={"stage": "review", "rationale": "Temporary assignment remains active."},
    )
    assert before_expiry.status_code == 200, before_expiry.text

    time.sleep(max(0, (expiry - datetime.now(UTC)).total_seconds()) + 0.15)
    after_expiry = client.post(
        f"/api/governance/claims/{after_claim['id']}/decisions",
        json={"stage": "review", "rationale": "Expired temporary access must be denied."},
    )
    assert after_expiry.status_code == 403, after_expiry.text


def test_template_version_requires_separate_admin_approval_and_revokes_old_grants(client):
    login(client, ADMIN_EMAIL)
    finance = _department(client, "Finance template version scope")
    users = {row["email"]: row["id"] for row in client.get("/api/admin/users").json()["users"]}
    for email in (USER_EMAIL, USER2_EMAIL):
        updated = client.patch(
            f"/api/admin/users/{users[email]}",
            json={"role": "employee", "department_ids": [finance]},
        )
        assert updated.status_code == 200, updated.text

    template = client.post(
        "/api/admin/role-templates",
        json={
            "name": "Finance version reviewer",
            "description": "Review Finance claims.",
            "base_role": "employee",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert template.status_code == 201, template.text
    template_id = template.json()["id"]
    assignment = client.post(
        f"/api/admin/role-templates/{template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert assignment.status_code == 201, assignment.text
    assignment_id = assignment.json()["id"]

    login(client, USER_EMAIL)
    before = _claim(client, finance, "Claim reviewed under role template version one.")
    login(client, USER2_EMAIL)
    allowed = client.post(
        f"/api/governance/claims/{before['id']}/decisions",
        json={"stage": "review", "rationale": "The original scoped reviewer may review."},
    )
    assert allowed.status_code == 200, allowed.text

    login(client, ADMIN_EMAIL)
    proposed = client.post(
        f"/api/admin/role-templates/{template_id}/change-requests",
        json={
            "description": "Review and publish Finance claims after review.",
            "permissions": [
                {"permission": "knowledge.review", "categories": ["financial"]},
                {"permission": "knowledge.publish", "categories": ["financial"]},
            ],
        },
    )
    assert proposed.status_code == 201, proposed.text
    request_id = proposed.json()["id"]
    assert proposed.json()["status"] == "pending"
    assert client.get("/api/admin/role-templates").json()["templates"]
    self_approval = client.post(
        f"/api/admin/role-template-change-requests/{request_id}/decision",
        json={"decision": "approve", "rationale": "Must be rejected as self approval."},
    )
    assert self_approval.status_code == 403, self_approval.text
    still_active = next(row for row in client.get("/api/admin/role-templates").json()["templates"] if row["id"] == template_id)
    assert still_active["is_active"] is True and still_active["version"] == 1

    second_admin_email = "independent-role-reviewer@example.com"
    second_admin_id = str(uuid.uuid4())
    run_db(lambda conn: conn.execute(
        text(
            "INSERT INTO users (id, email, password_hash, role, is_active) "
            "SELECT CAST(:id AS uuid), :email, password_hash, 'admin', true FROM users WHERE email=:admin"
        ),
        {"id": second_admin_id, "email": second_admin_email, "admin": ADMIN_EMAIL},
    ))
    login(client, second_admin_email)
    pending = client.get("/api/admin/role-template-change-requests")
    assert pending.status_code == 200, pending.text
    listed = next(row for row in pending.json()["requests"] if row["id"] == request_id)
    assert listed["requested_by_email"] == ADMIN_EMAIL and listed["status"] == "pending"
    approved = client.post(
        f"/api/admin/role-template-change-requests/{request_id}/decision",
        json={"decision": "approve", "rationale": "Independent review approved the expanded Finance scope."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["current_version"] == 1
    new_template_id = approved.json()["new_template_id"]
    assert new_template_id and approved.json()["decided_by_email"] == second_admin_email

    templates = client.get("/api/admin/role-templates").json()["templates"]
    old_template = next(row for row in templates if row["id"] == template_id)
    new_template = next(row for row in templates if row["id"] == new_template_id)
    assert old_template["is_active"] is False and old_template["version"] == 1
    assert new_template["is_active"] is True and new_template["version"] == 2
    assert new_template["supersedes_id"] == template_id
    # The active-assignment endpoint omits revoked history; confirm revocation and linked grant in PostgreSQL.
    assert run_db(lambda conn: conn.scalar(text(
        "SELECT a.revoked_at IS NOT NULL AND g.revoked_at IS NOT NULL "
        "FROM role_template_assignments a JOIN permission_grants g ON g.role_template_assignment_id=a.id "
        "WHERE a.id=CAST(:id AS uuid)"
    ), {"id": assignment_id})) is True

    login(client, USER_EMAIL)
    after = _claim(client, finance, "Claim denied after version-one grants are revoked.")
    login(client, USER2_EMAIL)
    denied_after_rotation = client.post(
        f"/api/governance/claims/{after['id']}/decisions",
        json={"stage": "review", "rationale": "Superseded-version grants must no longer authorize."},
    )
    assert denied_after_rotation.status_code == 403, denied_after_rotation.text

    login(client, second_admin_email)
    replacement = client.post(
        f"/api/admin/role-templates/{new_template_id}/assignments",
        json={"email": USER2_EMAIL, "department_ids": [finance], "expires_at": _expiry()},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["grant_count"] == 2

    login(client, ADMIN_EMAIL)
    rejected_proposal = client.post(
        f"/api/admin/role-templates/{new_template_id}/change-requests",
        json={
            "description": "Rejected proposal remains history only.",
            "permissions": [{"permission": "knowledge.review", "categories": ["financial"]}],
        },
    )
    assert rejected_proposal.status_code == 201, rejected_proposal.text
    reject_id = rejected_proposal.json()["id"]
    login(client, second_admin_email)
    rejected = client.post(
        f"/api/admin/role-template-change-requests/{reject_id}/decision",
        json={"decision": "reject", "rationale": "Scope change lacks sufficient business justification."},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    final_templates = client.get("/api/admin/role-templates").json()["templates"]
    assert sum(1 for row in final_templates if row["name"] == "Finance version reviewer" and row["is_active"]) == 1
    assert sum(1 for row in final_templates if row["name"] == "Finance version reviewer") == 2
    assert run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM role_template_change_requests WHERE status IN ('approved','rejected')"
    ))) == 2
