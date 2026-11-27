"""An invitation can carry a custom role that is assigned automatically when the invitee signs up."""

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, login, run_db

HR_OFFICER = {
    "name": "HR Officer",
    "base_role": "employee",
    "permissions": [{"permission": "people.invite"}, {"permission": "reports.view"}],
}


def _setup(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    role = client.post("/api/admin/role-templates", json=HR_OFFICER).json()
    return finance, role


def _accept(client, token: str, email: str):
    client.cookies.clear()
    accepted = client.post("/api/onboarding/accept", json={"token": token, "password": "new-hire-password-123"}, headers=CSRF)
    assert accepted.status_code in (200, 201), accepted.text
    client.cookies.clear()
    login(client, email, "new-hire-password-123")
    return client.get("/api/auth/me").json()


def test_invitation_with_custom_role_assigns_it_at_sign_up(client):
    finance, role = _setup(client)
    invited = client.post("/api/admin/users/invitations", json={
        "email": "hr.new@new.test", "role": "employee", "department_ids": [finance["id"]], "role_template_id": role["id"],
    })
    assert invited.status_code == 201, invited.text
    assert invited.json()["role_template_id"] == role["id"]

    me = _accept(client, invited.json()["token"], "hr.new@new.test")
    assert me["role"] == "employee"
    assert set(me["capabilities"]) == {"people.invite", "reports.view"}
    assert [g["name"] for g in client.get("/api/departments/invitable").json()["groups"]] == ["Finance"]


def test_custom_role_invitation_is_validated(client):
    finance, role = _setup(client)
    def invite(**overrides):
        payload = {"email": "x@new.test", "role": "employee", "department_ids": [finance["id"]], "role_template_id": role["id"], **overrides}
        return client.post("/api/admin/users/invitations", json=payload)

    assert invite(role="manager").status_code == 422  # role is for employee accounts
    assert invite(department_ids=[]).status_code == 422  # a custom role needs a department
    assert client.patch(f"/api/admin/role-templates/{role['id']}/active", json={"active": False}).status_code == 200
    assert invite().status_code == 422  # inactive role


def test_sign_up_still_succeeds_when_the_inviter_is_no_longer_an_administrator(client):
    finance, role = _setup(client)
    invited = client.post("/api/admin/users/invitations", json={
        "email": "late@new.test", "role": "employee", "department_ids": [finance["id"]], "role_template_id": role["id"],
    }).json()
    run_db(lambda c: c.execute(text("UPDATE users SET role = 'employee' WHERE email = :e"), {"e": ADMIN_EMAIL}))

    me = _accept(client, invited["token"], "late@new.test")
    assert me["capabilities"] == []  # not applied without an active administrator behind it
    actions = run_db(lambda c: c.execute(text("SELECT action FROM audit_logs WHERE target = 'late@new.test' ORDER BY id")))
    assert "onboarding.custom_role_not_applied" in [row[0] for row in actions.all()]
