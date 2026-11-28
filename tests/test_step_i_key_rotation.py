"""Step I slice 5: master-key rotation support.

* RotatingCredentialCipher decrypts values sealed under the current key or
  any retired key, encrypts only under the current key, and refuses unknown
  key ids — so a rotated key never makes stored credentials undecryptable.
* rotate_provider_credentials() re-encrypts stale provider credentials
  under the current key, audits each one, and leaves undecryptable rows
  exactly as they were (reported as skipped, never deleted or guessed at).
* Settings refuse an inconsistent previous-key pair at startup.
* The JSON sealed-payload helper re-encrypts {"ct","iv","kid"} dicts
  (integrations / workflows / agent runs) per payload.
"""

import asyncio
import base64

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.crypto import (
    CredentialCipher,
    DecryptionError,
    EncryptedValue,
    RotatingCredentialCipher,
    credential_aad,
    rotating_cipher_from_settings,
)
from app.core.config import get_settings
from app.db import session as db_session
from app.security.key_rotation import (
    SEALED_STORES,
    count_stale_provider_credentials,
    reencrypt_sealed_json,
    rotate_provider_credentials,
)
from tests.conftest import TEST_DATABASE_URL, TEST_MASTER_KEY, run_db

OLD_KEY = base64.b64decode(TEST_MASTER_KEY)  # b"k" * 32, key id "v1" in tests
NEW_KEY = b"n" * 32
THIRD_KEY = b"t" * 32


def _rotating() -> RotatingCredentialCipher:
    return RotatingCredentialCipher(NEW_KEY, "v2", {"v1": OLD_KEY})


def _insert_credential(*, provider: str, status: str, ciphertext: bytes,
                       nonce: bytes, key_id: str) -> int:
    async def _q(conn):
        config_id = (
            await conn.execute(
                text(
                    "INSERT INTO provider_configs (id, provider, model, is_active) "
                    "VALUES (gen_random_uuid(), :provider, 'm', false) RETURNING id"
                ),
                {"provider": provider},
            )
        ).scalar_one()
        credential_id = (
            await conn.execute(
                text(
                    "INSERT INTO provider_credentials "
                    "(id, provider_config_id, status, ciphertext, nonce, key_id, "
                    " key_hint, verified_at) "
                    "VALUES (gen_random_uuid(), :config_id, :status, :ciphertext, "
                    " :nonce, :key_id, 'abcd', now()) RETURNING id"
                ),
                {
                    "config_id": config_id,
                    "status": status,
                    "ciphertext": ciphertext,
                    "nonce": nonce,
                    "key_id": key_id,
                },
            )
        ).scalar_one()
        return credential_id

    return run_db(_q)


def _credential_row(credential_id: int) -> dict:
    async def _q(conn):
        row = (
            await conn.execute(
                text(
                    "SELECT status, key_id, ciphertext, nonce "
                    "FROM provider_credentials WHERE id = :id"
                ),
                {"id": credential_id},
            )
        ).one()
        return {"status": row[0], "key_id": row[1],
                "ciphertext": row[2], "nonce": row[3]}

    return run_db(_q)


def _run_sweep(cipher):
    # Fresh engine/loop per call (same pattern as conftest.run_db): the app's
    # sessionmaker belongs to the TestClient's loop and cannot be driven from
    # asyncio.run()'s new loop ("attached to a different loop").
    async def _sweep():
        engine = create_async_engine(
            TEST_DATABASE_URL, poolclass=NullPool, **db_session.test_engine_options()
        )
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await rotate_provider_credentials(db, cipher)
        finally:
            await engine.dispose()

    return asyncio.run(_sweep())


def _count_stale(cipher):
    async def _count():
        engine = create_async_engine(
            TEST_DATABASE_URL, poolclass=NullPool, **db_session.test_engine_options()
        )
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await count_stale_provider_credentials(db, cipher.key_id)
        finally:
            await engine.dispose()

    return asyncio.run(_count())


def _audit_rows(action: str) -> list[dict]:
    async def _q(conn):
        rows = (
            await conn.execute(
                text(
                    "SELECT actor_email, outcome, details FROM audit_logs "
                    "WHERE action = :action"
                ),
                {"action": action},
            )
        ).all()
        return [
            {"actor_email": a, "outcome": o, "details": d} for a, o, d in rows
        ]

    return run_db(_q)


# --- Rotating cipher: pure unit tests ----------------------------------------


def test_rotating_cipher_decrypts_retired_key_and_encrypts_current():
    old = CredentialCipher(OLD_KEY, "v1")
    sealed = old.encrypt("sk-test-123", associated_data=b"aad")
    assert sealed.key_id == "v1"

    rotating = _rotating()
    assert rotating.decrypt(sealed, associated_data=b"aad") == "sk-test-123"
    # New encryptions always use the current key.
    fresh = rotating.encrypt("sk-test-456", associated_data=b"aad")
    assert fresh.key_id == "v2"
    assert rotating.decrypt(fresh, associated_data=b"aad") == "sk-test-456"


def test_rotating_cipher_rejects_unknown_key_id():
    stranger = CredentialCipher(THIRD_KEY, "v9")
    sealed = stranger.encrypt("secret", associated_data=b"aad")
    with pytest.raises(DecryptionError, match="unknown key id"):
        _rotating().decrypt(sealed, associated_data=b"aad")


def test_rotating_cipher_rejects_retired_id_equal_to_current():
    with pytest.raises(ValueError, match="must differ"):
        RotatingCredentialCipher(NEW_KEY, "v2", {"v2": OLD_KEY})


def test_reencrypt_is_noop_when_already_current():
    rotating = _rotating()
    sealed = rotating.encrypt("secret", associated_data=b"aad")
    assert rotating.reencrypt(sealed, associated_data=b"aad") is sealed


def test_reencrypt_moves_stale_value_to_current_key():
    old = CredentialCipher(OLD_KEY, "v1")
    sealed = old.encrypt("secret", associated_data=b"aad")
    rotating = _rotating()
    moved = rotating.reencrypt(sealed, associated_data=b"aad")
    assert moved.key_id == "v2"
    assert moved is not sealed
    assert rotating.decrypt(moved, associated_data=b"aad") == "secret"
    # The old key alone can no longer read it; the current key can.
    with pytest.raises(DecryptionError):
        old.decrypt(moved, associated_data=b"aad")


def test_sealed_json_round_trip_and_reencryption():
    rotating = _rotating()
    sealed = rotating.encrypt_sealed_json('{"token": "abc"}', associated_data=b"aad")
    assert sealed["kid"] == "v2"
    assert rotating.decrypt_sealed_json(sealed, associated_data=b"aad") == (
        '{"token": "abc"}'
    )
    # Already current: no rewrite.
    assert reencrypt_sealed_json(sealed, rotating, associated_data=b"aad") is None

    old = CredentialCipher(OLD_KEY, "v1")
    stale = old.encrypt_sealed_json('{"token": "abc"}', associated_data=b"aad")
    assert stale["kid"] == "v1"
    moved = reencrypt_sealed_json(stale, rotating, associated_data=b"aad")
    assert moved is not None and moved["kid"] == "v2"
    assert rotating.decrypt_sealed_json(moved, associated_data=b"aad") == (
        '{"token": "abc"}'
    )


def test_sealed_json_reencryption_refuses_tampered_payload():
    rotating = _rotating()
    old = CredentialCipher(OLD_KEY, "v1")
    stale = old.encrypt_sealed_json('{"token": "abc"}', associated_data=b"aad")
    stale["ct"] = base64.b64encode(b"tampered-tampered!!").decode()
    with pytest.raises(DecryptionError):
        reencrypt_sealed_json(stale, rotating, associated_data=b"aad")


# --- Settings: previous-key pair validation ----------------------------------


def test_settings_reject_inconsistent_previous_key_pair():
    base = get_settings().model_dump()
    # Previous key without a previous id.
    with pytest.raises(ValidationError):
        type(get_settings()).model_validate(
            {**base,
             "master_encryption_key_previous": base64.b64encode(b"p" * 32).decode(),
             "master_encryption_key_previous_id": ""}
        )
    # Previous id equal to the current id.
    with pytest.raises(ValidationError):
        type(get_settings()).model_validate(
            {**base,
             "master_encryption_key_previous": base64.b64encode(b"p" * 32).decode(),
             "master_encryption_key_previous_id": base["master_encryption_key_id"]}
        )
    # Previous key that is not 32 bytes.
    with pytest.raises(ValidationError):
        type(get_settings()).model_validate(
            {**base,
             "master_encryption_key_previous": base64.b64encode(b"short").decode(),
             "master_encryption_key_previous_id": "v0"}
        )


def test_rotating_cipher_from_settings_with_and_without_previous():
    # Test env has no previous key: rotating cipher with no retired keys.
    plain = rotating_cipher_from_settings(get_settings())
    assert plain.key_id == "v1"
    assert plain.retired_key_ids == ()

    base = get_settings().model_dump()
    settings = type(get_settings()).model_validate(
        {**base,
         "master_encryption_key": base64.b64encode(NEW_KEY).decode(),
         "master_encryption_key_id": "v2",
         "master_encryption_key_previous": TEST_MASTER_KEY,
         "master_encryption_key_previous_id": "v1"}
    )
    rotating = rotating_cipher_from_settings(settings)
    assert rotating.key_id == "v2"
    assert rotating.retired_key_ids == ("v1",)
    old = CredentialCipher(OLD_KEY, "v1")
    sealed = old.encrypt("secret", associated_data=b"aad")
    assert rotating.decrypt(sealed, associated_data=b"aad") == "secret"


def test_sealed_stores_inventory_is_complete():
    names = {store.name for store in SEALED_STORES}
    assert names == {
        "provider_credentials",
        "integration sealed payloads (JSON ct/iv/kid)",
        "workflow sealed payloads (JSON ct/iv/kid)",
        "agent-run sealed task/result",
        "agent_studio_baileys sealed payloads",
    }
    assert [s for s in SEALED_STORES if s.name == "provider_credentials"][0].status == (
        "supported"
    )


# --- Sweep: database tests ----------------------------------------------------


def test_sweep_reencrypts_stale_provider_credential(admin_client):
    old = CredentialCipher(OLD_KEY, "v1")
    # The AAD is bound to the credential id, which only exists after the
    # insert — so insert a placeholder row, then update it with the
    # correctly-bound ciphertext.
    credential_id = _insert_credential(
        provider="deepseek-rotate",
        status="active",
        ciphertext=b"x",
        nonce=b"y",
        key_id="v1",
    )
    aad = credential_aad(credential_id, "deepseek-rotate", None)
    sealed = old.encrypt("sk-live-rotate-me", associated_data=aad)
    run_db(
        lambda c: c.execute(
            text(
                "UPDATE provider_credentials SET ciphertext = :ct, nonce = :n "
                "WHERE id = :id"
            ),
            {"ct": sealed.ciphertext, "n": sealed.nonce, "id": credential_id},
        )
    )

    rotating = _rotating()
    assert _count_stale(rotating) == 1

    result = _run_sweep(rotating)
    assert result.reencrypted == 1
    assert result.skipped == [] and result.failed == []
    assert result.done is True
    assert _count_stale(rotating) == 0

    row = _credential_row(credential_id)
    assert row["key_id"] == "v2"
    new_value = EncryptedValue(row["ciphertext"], row["nonce"], row["key_id"])
    assert rotating.decrypt(new_value, associated_data=aad) == "sk-live-rotate-me"

    events = _audit_rows("provider.credential_reencrypted")
    assert len(events) == 1
    assert events[0]["actor_email"] == "system"
    assert events[0]["outcome"] == "success"
    assert events[0]["details"]["credential_id"] == str(credential_id)
    assert events[0]["details"]["previous_key_id"] == "v1"
    assert events[0]["details"]["current_key_id"] == "v2"

    # Second sweep is a no-op: nothing stale remains.
    again = _run_sweep(rotating)
    assert again.reencrypted == 0 and again.done is True


def test_sweep_skips_undecryptable_row_and_leaves_it(admin_client):
    credential_id = _insert_credential(
        provider="deepseek-broken",
        status="active",
        ciphertext=b"garbage-not-a-valid-ciphertext!!",
        nonce=b"123456789012",
        key_id="v1",
    )
    before = _credential_row(credential_id)

    result = _run_sweep(_rotating())
    assert result.reencrypted == 0
    assert result.skipped == [credential_id]
    assert len(result.failed) == 1
    assert result.failed[0]["id"] == credential_id
    assert result.done is False

    after = _credential_row(credential_id)
    assert after == before  # left exactly as it was


def test_sweep_ignores_retired_credentials(admin_client):
    old = CredentialCipher(OLD_KEY, "v1")
    credential_id = _insert_credential(
        provider="deepseek-retired",
        status="retired",
        ciphertext=b"x",
        nonce=b"y",
        key_id="v1",
    )
    aad = credential_aad(credential_id, "deepseek-retired", None)
    sealed = old.encrypt("sk-old-retired", associated_data=aad)
    run_db(
        lambda c: c.execute(
            text(
                "UPDATE provider_credentials SET ciphertext = :ct, nonce = :n "
                "WHERE id = :id"
            ),
            {"ct": sealed.ciphertext, "n": sealed.nonce, "id": credential_id},
        )
    )

    rotating = _rotating()
    assert _count_stale(rotating) == 0
    result = _run_sweep(rotating)
    assert result.reencrypted == 0 and result.done is True
    assert _credential_row(credential_id)["key_id"] == "v1"  # untouched
