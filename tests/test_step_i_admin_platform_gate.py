"""Step I slice 3: /api/admin/* identity administration behind an explicit
platform gate (require_platform_admin -> org_gate "platform.manage").

Behaviour is preserved exactly for authorized users (the gate can only take
access away, never add it) and for everyone else (non-admins keep the same
403 require_admin gave). The tests prove the gate is actually consulted,
not just the legacy role check.
"""

import app.platform.modules as platform_modules
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login


def test_non_admin_still_gets_403_on_admin_users(client):
    login(client, USER_EMAIL)
    assert client.get("/api/admin/users").status_code == 403


def test_admin_lists_users_and_gate_is_consulted(client, monkeypatch):
    calls: list[str] = []
    original = platform_modules.org_gate

    async def spy(*args, **kwargs):
        # org_gate(db, user, permission, ...) takes the permission positionally.
        calls.append(args[2] if len(args) > 2 else kwargs.get("permission"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(platform_modules, "org_gate", spy)
    platform_modules.clear_diffs()
    login(client, ADMIN_EMAIL)
    response = client.get("/api/admin/users")
    assert response.status_code == 200, response.text
    assert "platform.manage" in calls
    # Legacy and platform agree for an active administrator: no shadow diff.
    assert platform_modules.recent_diffs() == []


def test_admin_role_guards_still_hold(client):
    # Self-change guard and demotion behaviour are service-level rules;
    # the gate migration must not disturb them.
    login(client, ADMIN_EMAIL)
    users = client.get("/api/admin/users").json()["users"]
    admin_id = next(u["id"] for u in users if u["email"] == ADMIN_EMAIL)
    refused = client.patch(f"/api/admin/users/{admin_id}", json={"role": "employee", "department_ids": []})
    assert refused.status_code == 403
