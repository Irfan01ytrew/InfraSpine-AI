"""Administrators can grant and revoke administrator rights in the app, with self-change and lockout guards."""

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db


def _user_id(client, email: str) -> str:
    return next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == email)


def _audit_actions(target: str) -> list[str]:
    rows = run_db(lambda c: c.execute(text("SELECT action FROM audit_logs WHERE target = :t ORDER BY id"), {"t": target}))
    return [row[0] for row in rows.all()]


def _session(client, email: str) -> dict[str, str]:
    """Sign in and return that account's cookies, so one client can act as several people."""
    client.cookies.clear()
    login(client, email)
    return dict(client.cookies)


def _as(client, cookies: dict[str, str]):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


def test_admin_grants_and_revokes_administrator_rights_with_immediate_effect(client):
    user = _session(client, USER_EMAIL)
    admin = _session(client, ADMIN_EMAIL)
    user_id = _user_id(client, USER_EMAIL)
    admin_id = _user_id(client, ADMIN_EMAIL)
    assert _as(client, user).get("/api/admin/users").status_code == 403

    granted = _as(client, admin).patch(f"/api/admin/users/{user_id}", json={"role": "admin", "department_ids": []})
    assert granted.status_code == 200, granted.text
    assert granted.json()["role"] == "admin"
    assert _as(client, user).get("/api/admin/users").status_code == 200  # the existing session gains rights now

    # Nobody can change their own role; the other administrator has to do it.
    assert _as(client, user).patch(f"/api/admin/users/{user_id}", json={"role": "employee", "department_ids": []}).status_code == 403
    assert _as(client, admin).patch(f"/api/admin/users/{admin_id}", json={"role": "employee", "department_ids": []}).status_code == 403
    revoked = _as(client, admin).patch(f"/api/admin/users/{user_id}", json={"role": "employee", "department_ids": []})
    assert revoked.status_code == 200, revoked.text
    assert _as(client, user).get("/api/auth/me").status_code == 401  # a demoted administrator's sessions end now

    assert _audit_actions(USER_EMAIL)[-2:] == ["user.administrator_granted", "user.administrator_revoked"]


def test_promotion_requires_an_active_account_and_clears_department_management(client):
    login(client, ADMIN_EMAIL)
    department = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    user_id = _user_id(client, USER2_EMAIL)
    assert client.patch(f"/api/admin/users/{user_id}", json={"role": "manager", "department_ids": [department["id"]]}).status_code == 200

    assert client.post(f"/api/admin/users/{user_id}/active", json={"active": False}).status_code == 200
    refused = client.patch(f"/api/admin/users/{user_id}", json={"role": "admin", "department_ids": []})
    assert refused.status_code == 422 and "Reactivate" in refused.json()["error"]["message"]

    assert client.post(f"/api/admin/users/{user_id}/active", json={"active": True}).status_code == 200
    assert client.patch(f"/api/admin/users/{user_id}", json={"role": "admin", "department_ids": []}).status_code == 200
    managers = run_db(lambda c: c.execute(text("SELECT count(*) FROM department_managers WHERE manager_user_id = :u"), {"u": user_id}))
    assert managers.scalar_one() == 0


def test_only_administrators_can_change_roles(client):
    login(client, USER_EMAIL)
    admin_id = run_db(lambda c: c.execute(text("SELECT id FROM users WHERE email = :e"), {"e": ADMIN_EMAIL})).scalar_one()
    assert client.patch(f"/api/admin/users/{admin_id}", json={"role": "employee", "department_ids": []}).status_code == 403
