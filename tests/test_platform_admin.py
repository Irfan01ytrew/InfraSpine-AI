"""Organisation settings API: the time zone that defines business days."""

from tests.conftest import CSRF, USER_EMAIL, login


def test_admin_reads_and_changes_the_org_time_zone(admin_client):
    first = admin_client.get("/api/admin/platform/org-settings")
    assert first.status_code == 200
    assert first.json()["time_zone"] == "Asia/Karachi" and first.json()["default_time_zone"] == "Asia/Karachi"
    zones = admin_client.get("/api/admin/platform/time-zones").json()["time_zones"]
    assert "Asia/Karachi" in zones and "UTC" in zones

    bad = admin_client.put("/api/admin/platform/org-settings", json={"time_zone": "Mars/Base"}, headers=CSRF)
    assert bad.status_code == 422 or bad.status_code == 400
    changed = admin_client.put("/api/admin/platform/org-settings", json={"time_zone": "UTC"}, headers=CSRF)
    assert changed.status_code == 200 and changed.json()["time_zone"] == "UTC"
    assert admin_client.get("/api/admin/platform/org-settings").json()["time_zone"] == "UTC"
    logs = admin_client.get("/api/admin/audit-logs").json()
    assert any(row.get("action") == "platform.org_time_zone_changed" for row in logs["entries"])


def test_non_admins_cannot_read_or_change_org_settings(client):
    login(client, USER_EMAIL)
    assert client.get("/api/admin/platform/org-settings").status_code == 403
    assert client.put("/api/admin/platform/org-settings", json={"time_zone": "UTC"}, headers=CSRF).status_code == 403
