"""Custom roles (role templates) can bundle every delegable capability, not only knowledge governance."""

from datetime import UTC, datetime, timedelta

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login


def _session(client, email: str) -> dict[str, str]:
    client.cookies.clear()
    login(client, email)
    return dict(client.cookies)


def _as(client, cookies: dict[str, str]):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


HR_OFFICER = {
    "name": "HR Officer",
    "description": "Onboards employees and follows usage",
    "base_role": "employee",
    "permissions": [
        {"permission": "people.invite"},
        {"permission": "reports.view"},
        {"permission": "knowledge.review", "categories": ["general"]},
    ],
}


def test_custom_role_validation_matches_each_permission_scope(admin_client):
    def create(permissions):
        return admin_client.post("/api/admin/role-templates", json={**HR_OFFICER, "name": "Bad role", "permissions": permissions})

    assert create([{"permission": "people.invite", "categories": ["general"]}]).status_code == 422  # not category-scoped
    assert create([{"permission": "knowledge.review"}]).status_code == 422  # needs a category
    assert create([{"permission": "reports.view"}, {"permission": "reports.view"}]).status_code == 422  # duplicate


def test_custom_role_bundles_department_and_organization_capabilities(client):
    user = _session(client, USER_EMAIL)
    admin = _session(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    user_id = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == USER_EMAIL)
    assert client.patch(f"/api/admin/users/{user_id}", json={"role": "employee", "department_ids": [finance["id"]]}).status_code == 200

    role = client.post("/api/admin/role-templates", json=HR_OFFICER)
    assert role.status_code == 201, role.text
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assigned = client.post(f"/api/admin/role-templates/{role.json()['id']}/assignments", json={
        "email": USER_EMAIL, "department_ids": [finance["id"]], "expires_at": expires,
    })
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["grant_count"] == 3  # invite in Finance + organization reports + review(general) in Finance

    hr = _as(client, user)
    assert set(hr.get("/api/auth/me").json()["capabilities"]) == {"people.invite", "reports.view"}
    assert [g["name"] for g in hr.get("/api/departments/invitable").json()["groups"]] == ["Finance"]
    assert hr.post(f"/api/departments/{finance['id']}/invitations", json={"email": "new.hire@new.test"}).status_code == 201
    assert hr.get("/api/admin/reports/usage?days=30").status_code == 200

    revoked = _as(client, admin).post(f"/api/admin/role-template-assignments/{assigned.json()['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    hr = _as(client, user)
    assert hr.get("/api/auth/me").json()["capabilities"] == []
    assert hr.get("/api/admin/reports/usage?days=30").status_code == 403
