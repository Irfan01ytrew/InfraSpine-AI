"""Organization usage reports can be delegated read-only with the reports.view permission."""

import uuid

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login


def _session(client, email: str) -> dict[str, str]:
    client.cookies.clear()
    login(client, email)
    return dict(client.cookies)


def _as(client, cookies: dict[str, str]):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


def test_reports_view_grant_opens_reports_read_only_and_revocation_closes_them(client):
    user = _session(client, USER_EMAIL)
    admin = _session(client, ADMIN_EMAIL)

    assert _as(client, user).get("/api/admin/reports/usage?days=30").status_code == 403
    assert _as(client, user).get("/api/auth/me").json()["capabilities"] == []
    assert "reports.view" in [p["permission"] for p in _as(client, admin).get("/api/admin/access/permission-catalog").json()["permissions"]]

    scoped = _as(client, admin).post("/api/admin/access/grants", json={
        "email": USER_EMAIL, "permission": "reports.view", "category": "financial", "scope_group_id": None,
    })
    assert scoped.status_code == 422  # organization-wide permission: no category or department scope
    grant = _as(client, admin).post("/api/admin/access/grants", json={
        "email": USER_EMAIL, "permission": "reports.view", "category": None, "scope_group_id": None,
    })
    assert grant.status_code in (200, 201), grant.text

    viewer = _as(client, user)
    assert viewer.get("/api/auth/me").json()["capabilities"] == ["reports.view"]
    assert viewer.get("/api/admin/reports/usage?days=30").status_code == 200
    assert viewer.get("/api/admin/reports/operational-health").status_code == 200
    assert viewer.get("/api/admin/reports/usage/export?days=30&format=csv").status_code == 200
    quota = viewer.put(f"/api/admin/reports/quotas/{uuid.uuid4()}", json={"monthly_token_quota": 1000, "alert_threshold_percent": 80})
    assert quota.status_code == 403  # changing quotas stays administrator-only
    assert viewer.get("/api/admin/users").status_code == 403  # no other administrator area opens

    revoked = _as(client, admin).post(f"/api/admin/access/grants/{grant.json()['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert _as(client, user).get("/api/admin/reports/usage?days=30").status_code == 403
    assert _as(client, user).get("/api/auth/me").json()["capabilities"] == []


def test_administrators_always_hold_report_capability(client):
    login(client, ADMIN_EMAIL)
    assert client.get("/api/auth/me").json()["capabilities"] == ["reports.view", "people.invite", "training.manage", "agents.manage"]
