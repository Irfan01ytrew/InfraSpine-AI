import asyncio
import logging

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.crypto import CredentialCipher, EncryptedValue, credential_aad
from app.main import app
from tests.conftest import (
    ADMIN_EMAIL,
    CSRF,
    INVALID_KEY,
    PASSWORD,
    TEST_MASTER_KEY,
    USER_EMAIL,
    VALID_KEY_1,
    VALID_KEY_2,
    login,
    run_db,
)

BASE = "/api/admin/providers/deepseek"


def state(client) -> dict:
    return client.get(BASE).json()


def configure(client, model="deepseek-flash", settings=None) -> dict:
    response = client.put(
        BASE, json={"model": model, "settings": settings or {}, "expected_version": state(client)["version"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def stage(client, key):
    return client.post(f"{BASE}/credentials", json={"api_key": key, "expected_version": state(client)["version"]})


def activate(client):
    return client.post(f"{BASE}/activate", json={"expected_version": state(client)["version"]})


def chat(client):
    return client.post("/api/ai/chat", json={"message": "hello"})


def credential_rows():
    return run_db(
        lambda c: c.execute(
            text("SELECT id, status, ciphertext, nonce, key_id, key_hint FROM provider_credentials ORDER BY created_at")
        )
    ).all()


def audit_actions():
    rows = run_db(lambda c: c.execute(text("SELECT action, outcome FROM audit_logs ORDER BY id")))
    return [tuple(r) for r in rows.all()]


# ---------------------------------------------------------------- metadata


def test_catalog_is_backend_driven(admin_client):
    provider = admin_client.get("/api/admin/providers/catalog").json()["providers"][0]
    assert provider["id"] == "deepseek"
    assert {m["id"] for m in provider["models"]} == {"deepseek-flash", "deepseek-v4-pro"}
    assert {s["key"] for s in provider["settings"]} == {"thinking", "temperature", "max_tokens"}
    flash = next(m for m in provider["models"] if m["id"] == "deepseek-flash")
    assert flash["context_window"] == 1_000_000 and flash["max_output_tokens"] == 384_000
    assert "client_factory" not in provider


def test_unconfigured_provider_state(admin_client):
    body = admin_client.get("/api/admin/providers").json()
    assert body["mode"] == "database"
    assert body["providers"][0] == {
        "provider": "deepseek",
        "display_name": "DeepSeek",
        "base_url": None,
        "configured": False,
        "model": "deepseek-flash",
        "settings": {"thinking": "enabled", "max_tokens": 32768, "temperature": 1.0},
        "is_active": False,
        "version": 0,
        "active_credential": None,
        "pending_credential": None,
        "updated_at": None,
        "image_sharing_authorized": False,
        "image_sharing_authorized_at": None,
    }


# --------------------------------------------------------------- validation


def test_config_validation(admin_client):
    bad_model = admin_client.put(BASE, json={"model": "gpt-x", "settings": {}, "expected_version": 0})
    assert bad_model.status_code == 422
    bad_range = admin_client.put(
        BASE, json={"model": "deepseek-flash", "settings": {"temperature": 5}, "expected_version": 0}
    )
    assert bad_range.status_code == 422
    assert bad_range.json()["error"]["details"]["errors"]
    unknown = admin_client.put(
        BASE, json={"model": "deepseek-flash", "settings": {"top_k": 1}, "expected_version": 0}
    )
    assert unknown.status_code == 422
    too_many_tokens = admin_client.put(
        BASE, json={"model": "deepseek-flash", "settings": {"max_tokens": 384_001}, "expected_version": 0}
    )
    assert too_many_tokens.status_code == 422
    bad_option = admin_client.put(
        BASE, json={"model": "deepseek-flash", "settings": {"thinking": "maybe"}, "expected_version": 0}
    )
    assert bad_option.status_code == 422
    unknown_provider = admin_client.get("/api/admin/providers/nope")
    assert unknown_provider.status_code == 404


def test_modern_token_limits_are_accepted(admin_client):
    body = configure(admin_client, settings={"max_tokens": 384_000, "thinking": "disabled"})
    assert body["settings"] == {"thinking": "disabled", "max_tokens": 384_000, "temperature": 1.0}


def test_stale_version_is_rejected(admin_client):
    configure(admin_client)
    response = admin_client.put(BASE, json={"model": "deepseek-flash", "settings": {}, "expected_version": 0})
    assert response.status_code == 409


def test_key_cannot_be_staged_before_configuration(admin_client):
    response = admin_client.post(f"{BASE}/credentials", json={"api_key": VALID_KEY_1, "expected_version": 1})
    assert response.status_code == 409


# -------------------------------------------------------- credential flow


def test_invalid_key_is_never_stored(admin_client):
    configure(admin_client)
    response = stage(admin_client, INVALID_KEY)
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"reason": "invalid_credentials"}
    assert credential_rows() == []
    assert ("provider.credential_stage", "failure") in audit_actions()


def test_staged_key_is_encrypted_and_never_returned(admin_client):
    configure(admin_client)
    response = stage(admin_client, VALID_KEY_1)
    assert response.status_code == 200
    body = response.json()
    assert body["pending_credential"]["key_hint"] == VALID_KEY_1[-4:]
    assert body["active_credential"] is None

    for text_ in (response.text, admin_client.get("/api/admin/providers").text, admin_client.get(BASE).text,
                  admin_client.get("/api/admin/audit-logs").text):
        assert VALID_KEY_1 not in text_

    [row] = credential_rows()
    assert row.status == "pending"
    assert VALID_KEY_1.encode() not in row.ciphertext
    # And it is genuinely decryptable with the master key + row binding.
    cipher = CredentialCipher(__import__("base64").b64decode(TEST_MASTER_KEY), "v1")
    plaintext = cipher.decrypt(
        EncryptedValue(row.ciphertext, row.nonce, row.key_id), associated_data=credential_aad(row.id, "deepseek")
    )
    assert plaintext == VALID_KEY_1


def test_pending_key_does_not_serve_traffic_until_activated(admin_client):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    assert chat(admin_client).json()["error"]["code"] == "llm_not_configured"
    assert activate(admin_client).status_code == 200
    reply = chat(admin_client)
    assert reply.status_code == 200
    assert reply.json()["reply"].startswith(f"key={VALID_KEY_1[-4:]}")


def test_rotation_switches_keys_at_runtime_and_shreds_the_old_one(admin_client, fake_provider):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    assert chat(admin_client).json()["reply"].startswith(f"key={VALID_KEY_1[-4:]}")

    stage(admin_client, VALID_KEY_2)
    # Staged but not yet active: traffic still uses key 1.
    assert chat(admin_client).json()["reply"].startswith(f"key={VALID_KEY_1[-4:]}")
    body = activate(admin_client).json()
    assert body["active_credential"]["key_hint"] == VALID_KEY_2[-4:]
    assert body["pending_credential"] is None
    # No restart: the very next request uses the new key.
    assert chat(admin_client).json()["reply"].startswith(f"key={VALID_KEY_2[-4:]}")

    rows = credential_rows()
    assert [r.status for r in rows] == ["retired", "active"]
    assert rows[0].ciphertext is None and rows[0].nonce is None


def test_failed_rotation_leaves_active_key_untouched(admin_client):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    before = state(admin_client)

    assert stage(admin_client, INVALID_KEY).status_code == 400
    after = state(admin_client)
    assert after["active_credential"] == before["active_credential"]
    assert after["version"] == before["version"]
    assert chat(admin_client).json()["reply"].startswith(f"key={VALID_KEY_1[-4:]}")


def test_activation_reverifies_and_refuses_a_revoked_key(admin_client, fake_provider):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    stage(admin_client, VALID_KEY_2)
    fake_provider.revoked_keys.add(VALID_KEY_2)  # revoked at the provider after staging

    response = activate(admin_client)
    assert response.status_code == 400
    current = state(admin_client)
    assert current["active_credential"]["key_hint"] == VALID_KEY_1[-4:]
    assert current["pending_credential"]["key_hint"] == VALID_KEY_2[-4:]
    assert chat(admin_client).json()["reply"].startswith(f"key={VALID_KEY_1[-4:]}")
    assert ("provider.activate", "failure") in audit_actions()


def test_model_and_settings_changes_apply_without_restart(admin_client, fake_provider):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    assert chat(admin_client).json()["model"] == "deepseek-flash"

    # A model the active key can't use is refused, and the live config is unchanged.
    fake_provider.available_models = {"deepseek-flash"}
    response = admin_client.put(
        BASE, json={"model": "deepseek-v4-pro", "settings": {}, "expected_version": state(admin_client)["version"]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"reason": "model_unavailable"}
    assert state(admin_client)["model"] == "deepseek-flash"

    fake_provider.available_models = {"deepseek-flash", "deepseek-v4-pro"}
    configure(admin_client, model="deepseek-v4-pro", settings={"temperature": 0.2})
    reply = chat(admin_client).json()
    assert reply["model"] == "deepseek-v4-pro"
    assert "temp=0.2" in reply["reply"]


def test_deactivate_stops_traffic(admin_client):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    response = admin_client.post(f"{BASE}/deactivate", json={"expected_version": state(admin_client)["version"]})
    assert response.status_code == 200
    assert chat(admin_client).status_code == 503
    # Re-activating the existing (re-verified) key works.
    assert activate(admin_client).status_code == 200
    assert chat(admin_client).status_code == 200


def test_connection_test_never_persists_the_key(admin_client):
    ok = admin_client.post(f"{BASE}/test", json={"api_key": VALID_KEY_1}).json()
    assert ok["ok"] is True and ok["tested"] == "provided_key"
    bad = admin_client.post(f"{BASE}/test", json={"api_key": INVALID_KEY}).json()
    assert bad == {**bad, "ok": False, "reason": "invalid_credentials"}
    assert credential_rows() == []

    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    stored = admin_client.post(f"{BASE}/test", json={}).json()
    assert stored["ok"] is True and stored["tested"] == "pending_key"


def test_secrets_never_reach_logs(admin_client, caplog):
    caplog.set_level(logging.DEBUG)
    configure(admin_client)
    stage(admin_client, INVALID_KEY)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)
    stage(admin_client, VALID_KEY_2)
    activate(admin_client)
    admin_client.post(f"{BASE}/test", json={"api_key": VALID_KEY_1})
    chat(admin_client)
    for secret in (VALID_KEY_1, VALID_KEY_2, INVALID_KEY, PASSWORD):
        assert secret not in caplog.text


# ------------------------------------------------------------- concurrency


def test_concurrent_rotations_one_wins_one_conflicts(admin_client, fake_provider):
    configure(admin_client)
    version = state(admin_client)["version"]
    fake_provider.verify_delay = 0.2  # both requests verify at the same time

    async def race():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as http:
                await http.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
                return await asyncio.gather(
                    *(
                        http.post(f"{BASE}/credentials", json={"api_key": key, "expected_version": version})
                        for key in (VALID_KEY_1, VALID_KEY_2)
                    )
                )

    responses = asyncio.run(race())
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert [r.status for r in credential_rows()] == ["pending"]


def test_database_enforces_single_active_provider_and_credential(admin_client):
    configure(admin_client)
    stage(admin_client, VALID_KEY_1)
    activate(admin_client)

    async def second_active_provider(conn):
        await conn.execute(
            text("INSERT INTO provider_configs (id, provider, model, is_active) VALUES (gen_random_uuid(), 'other', 'm', true)")
        )

    with pytest.raises(IntegrityError):
        run_db(second_active_provider)

    async def second_active_key(conn):
        await conn.execute(
            text(
                "INSERT INTO provider_credentials (id, provider_config_id, status, ciphertext, nonce, key_id, key_hint, verified_at) "
                "SELECT gen_random_uuid(), id, 'active', 'x', 'y', 'v1', 'abcd', now() FROM provider_configs"
            )
        )

    with pytest.raises(IntegrityError):
        run_db(second_active_key)

    async def unverified_key(conn):
        await conn.execute(
            text(
                "INSERT INTO provider_credentials (id, provider_config_id, status, ciphertext, nonce, key_id, key_hint) "
                "SELECT gen_random_uuid(), id, 'retired', NULL, NULL, 'v1', 'abcd' FROM provider_configs"
            )
        )

    with pytest.raises(IntegrityError):  # verified_at is NOT NULL
        run_db(unverified_key)


def test_audit_log_is_append_only(admin_client):
    configure(admin_client)
    with pytest.raises(DBAPIError, match="append-only"):
        run_db(lambda c: c.execute(text("UPDATE audit_logs SET outcome = 'failure'")))
    with pytest.raises(DBAPIError, match="append-only"):
        run_db(lambda c: c.execute(text("DELETE FROM audit_logs")))


def test_admin_removes_an_inactive_provider_and_its_keys(client):
    from tests.rag_helpers import PROVIDER, activate_provider

    activate_provider(client)  # leaves the admin signed in
    version = client.get(PROVIDER).json()["version"]
    assert client.delete(f"{PROVIDER}?expected_version={version}").status_code == 409  # active: deactivate first
    assert client.post(f"{PROVIDER}/deactivate", json={"expected_version": version}).status_code == 200
    version = client.get(PROVIDER).json()["version"]
    assert client.delete(f"{PROVIDER}?expected_version={version - 1}").status_code == 409  # stale version

    login(client, USER_EMAIL)
    assert client.delete(f"{PROVIDER}?expected_version={version}").status_code == 403
    login(client, ADMIN_EMAIL)
    assert client.delete(f"{PROVIDER}?expected_version={version}").status_code == 204

    state = client.get(PROVIDER).json()
    assert state["configured"] is False and state["active_credential"] is None  # built-in: back to "not configured"
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM provider_credentials"))) == 0  # keys deleted
    assert client.get("/api/rag/models").json()["options"] == []
    assert client.delete(f"{PROVIDER}?expected_version={version}").status_code == 404
    actions = [r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs"))).all()]
    assert "provider.removed" in actions
