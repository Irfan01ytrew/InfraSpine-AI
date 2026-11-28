"""Step AF slice 5: alert configuration API. Real PostgreSQL *_test only.

Covers: defaults for an unconfigured scope; admin CRUD with validation
(unknown rule, bad threshold/cooldown, bad recipient, unknown channel);
invalid payloads change nothing; non-admins are refused; changes are
audited; delivery history is visible to scope members only.
"""

from uuid import uuid4

from sqlalchemy import text

from app.control_room.config import (
    config_defaults,
    config_to_dict,
    effective_rules,
    validate_config_payload,
)
from tests.conftest import login, run_db
from tests.test_control_room_charts import seed_scope

ADMIN = "admin@example.com"
USER = "user@example.com"
USER2 = "user2@example.com"


def _config(client, scope, method="get", payload=None):
    login(client, ADMIN)
    if method == "get":
        return client.get(f"/api/control-room/scopes/{scope}/config")
    return client.put(f"/api/control-room/scopes/{scope}/config", json=payload)


def _audit_actions():
    async def query(conn):
        rows = (await conn.execute(text("SELECT action FROM audit_logs"))).all()
        return [r[0] for r in rows]

    return run_db(query)


def _seed_delivery(scope):
    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO control_room_alert_deliveries"
                "(id,scope_id,rule_key,severity,title,detail,channel,recipient,status,evaluated_at)"
                " VALUES (:id,:s,'failed_runs_spike','warning','Failed runs spiking','t','whatsapp','+923001234567','sent',now())"
            ),
            {"id": uuid4(), "s": scope},
        )

    run_db(seed)


def test_config_defaults_for_unconfigured_scope(client):
    scope, _ = seed_scope(client)
    response = _config(client, scope)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scope_id"] == scope
    assert data["enabled"] is True
    assert data["quiet_start"] == "22:00"
    assert data["quiet_end"] == "07:00"
    assert data["recipients"] == []
    assert data["rules"] == {}
    # The merged rule catalogue the admin UI renders: one row per rule.
    rows = data["effective_rules"]
    assert len(rows) == 13
    spike = next(row for row in rows if row["key"] == "failed_runs_spike")
    assert spike["title"] == "Failed runs spiking"
    assert spike["threshold"] == spike["default_threshold"] == 5
    assert spike["cooldown_seconds"] == spike["default_cooldown_seconds"] == 3600


def test_config_put_and_get_roundtrip(client):
    scope, _ = seed_scope(client)
    payload = {
        "enabled": True,
        "quiet_start": "23:00",
        "quiet_end": "06:00",
        "recipients": [
            {"channel": "whatsapp", "address": "+923001234567"},
            {"channel": "email", "address": "owner@example.com"},
        ],
        "rules": {"failed_runs_spike": {"threshold": 10, "cooldown_seconds": 1800}},
    }
    response = _config(client, scope, method="put", payload=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["quiet_start"] == "23:00"
    assert data["recipients"][0] == {"channel": "whatsapp", "address": "+923001234567"}
    assert data["rules"]["failed_runs_spike"]["threshold"] == 10

    again = _config(client, scope)
    assert again.json()["rules"]["failed_runs_spike"]["cooldown_seconds"] == 1800


def test_config_rejects_unknown_rule_key(client):
    scope, _ = seed_scope(client)
    response = _config(client, scope, method="put", payload={"rules": {"nope": {"threshold": 3}}})
    assert response.status_code == 422
    assert _config(client, scope).json()["rules"] == {}


def test_config_rejects_bad_thresholds_and_cooldowns(client):
    scope, _ = seed_scope(client)
    for payload in (
        {"rules": {"failed_runs_spike": {"threshold": 0}}},
        {"rules": {"failed_runs_spike": {"threshold": -1}}},
        {"rules": {"failed_runs_spike": {"threshold": 1.5}}},
        {"rules": {"failed_runs_spike": {"cooldown_seconds": 0}}},
        {"rules": {"failed_runs_spike": {"cooldown_seconds": -60}}},
        {"rules": {"failed_runs_spike": {"enabled": "yes"}}},
    ):
        response = _config(client, scope, method="put", payload=payload)
        assert response.status_code == 422, payload
    assert _config(client, scope).json()["rules"] == {}


def test_config_rejects_bad_recipients_and_channels(client):
    scope, _ = seed_scope(client)
    for payload in (
        {"recipients": [{"channel": "whatsapp", "address": "03001234567"}]},
        {"recipients": [{"channel": "whatsapp", "address": "+92"}]},
        {"recipients": [{"channel": "email", "address": "not-an-email"}]},
        {"recipients": [{"channel": "sms", "address": "+923001234567"}]},
        {"recipients": [{"channel": "in_app", "address": "anyone"}]},
        {"recipients": [{"channel": "whatsapp", "address": "+923001234567"}] * 11},
        {"recipients": [{"channel": "whatsapp"}]},
        {"recipients": ["+923001234567"]},
        {"rules": {"failed_runs_spike": {"channels": ["sms"]}}},
        {"rules": {"failed_runs_spike": {"channels": []}}},
    ):
        response = _config(client, scope, method="put", payload=payload)
        assert response.status_code == 422, payload
    assert _config(client, scope).json()["recipients"] == []


def test_config_rejects_bad_quiet_hours(client):
    scope, _ = seed_scope(client)
    for payload in ({"quiet_start": "25:00"}, {"quiet_end": "7am"}, {"quiet_start": "22:00", "quiet_end": "22:00"}):
        response = _config(client, scope, method="put", payload=payload)
        assert response.status_code == 422, payload
    assert _config(client, scope).json()["quiet_start"] == "22:00"


def test_config_rejects_unknown_fields(client):
    scope, _ = seed_scope(client)
    response = _config(client, scope, method="put", payload={"surprise": True})
    assert response.status_code == 422
    assert _config(client, scope).json()["enabled"] is True


def test_config_non_admin_is_403_and_unknown_scope_404(client):
    scope, _ = seed_scope(client)
    login(client, USER)
    response = client.put(f"/api/control-room/scopes/{scope}/config", json={"enabled": False})
    assert response.status_code == 403

    login(client, ADMIN)
    response = client.put(f"/api/control-room/scopes/{uuid4()}/config", json={"enabled": False})
    assert response.status_code == 404


def test_config_change_is_audited(client):
    scope, _ = seed_scope(client)
    _config(client, scope, method="put", payload={"enabled": False})
    assert "control_room.alert_config_changed" in _audit_actions()


def test_deliveries_visible_to_members_only(client):
    scope, _ = seed_scope(client)
    _seed_delivery(scope)

    login(client, ADMIN)
    response = client.get(f"/api/control-room/scopes/{scope}/deliveries")
    assert response.status_code == 200, response.text
    rows = response.json()["deliveries"]
    assert len(rows) == 1
    assert rows[0]["rule_key"] == "failed_runs_spike"
    assert rows[0]["channel"] == "whatsapp"
    assert rows[0]["status"] == "sent"

    login(client, USER2)  # seeded user, not a member of this scope
    response = client.get(f"/api/control-room/scopes/{scope}/deliveries")
    assert response.status_code == 403

    login(client, ADMIN)
    response = client.get(f"/api/control-room/scopes/{uuid4()}/deliveries")
    assert response.status_code == 404


def test_deliveries_limit_is_bounded(client):
    scope, _ = seed_scope(client)
    login(client, ADMIN)
    for limit in ("0", "201", "abc"):
        response = client.get(f"/api/control-room/scopes/{scope}/deliveries?limit={limit}")
        assert response.status_code == 422, limit
    response = client.get(f"/api/control-room/scopes/{scope}/deliveries?limit=200")
    assert response.status_code == 200


# --- Pure helpers (no DB) ---------------------------------------------------


def test_effective_rules_merge_overrides_and_ignore_unknowns():
    stored = {
        "failed_runs_spike": {"threshold": 10},
        "ingestion_down": {"enabled": False},
        "not_a_rule": {"threshold": 1},
    }
    # effective_rules takes a config dict (the shape config_to_dict and
    # every production caller produce), not a bare rules mapping.
    rules = {rule.key: rule for rule in effective_rules({"rules": stored})}
    assert rules["failed_runs_spike"].threshold == 10
    assert rules["failed_runs_spike"].cooldown_seconds == 3600  # default kept
    assert rules["ingestion_down"].enabled is False
    assert "not_a_rule" not in rules
    # Severity is not configurable: the override cannot change it.
    assert rules["failed_runs_spike"].severity == "warning"


def test_validate_config_payload_collects_problems():
    problems = validate_config_payload(
        {
            "rules": {"nope": {"threshold": 0}},
            "recipients": [{"channel": "sms", "address": "x"}],
            "quiet_start": "99:99",
        }
    )
    assert len(problems) >= 3


def test_config_defaults_and_to_dict_roundtrip():
    defaults = config_defaults()
    assert defaults["enabled"] is True
    assert defaults["quiet_start"] == "22:00"
    data = config_to_dict(None)
    assert data == defaults
