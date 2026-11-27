"""TOTP MFA setup, one-use recovery and bounded second-factor login; all accounts are synthetic."""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import text

from app.core import totp
from app.services import auth
from tests.conftest import ADMIN_EMAIL, PASSWORD, login, run_db

_FIXED_TIME = 1_800_000_000


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_FIXED_TIME, tz=tz or UTC)


def test_totp_rfc_vector_and_replay_window():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.totp_code(secret, 1, digits=8) == "94287082"
    assert totp.totp_code(secret, 2, digits=8) == "37359152"
    code = totp.totp_code(secret, 1)
    assert totp.matching_totp_counter(secret, code, now=59, after_counter=-1) == 1
    assert totp.matching_totp_counter(secret, code, now=59, after_counter=1) is None
    assert totp.matching_totp_counter(secret, "abcdef", now=59) is None


def _enable_mfa(client, monkeypatch):
    monkeypatch.setattr(auth, "datetime", _FrozenDateTime)
    login(client, ADMIN_EMAIL, PASSWORD)
    started = client.post("/api/auth/mfa/enrollment", json={"password": PASSWORD})
    assert started.status_code == 200, started.text
    data = started.json()
    secret = data["secret"]
    assert "otpauth://totp/" in data["provisioning_uri"]

    enrollment_code = totp.totp_code(secret, int(_FIXED_TIME // totp.TOTP_PERIOD_SECONDS) - 1)
    confirmed = client.post("/api/auth/mfa/enrollment/confirm", json={"code": enrollment_code})
    assert confirmed.status_code == 200, confirmed.text
    recovery_codes = confirmed.json()["recovery_codes"]
    assert len(recovery_codes) == 10 and len(set(recovery_codes)) == 10

    async def read_stored_factor(conn):
        ciphertext = (
            await conn.execute(text("SELECT secret_ciphertext FROM user_mfa_factors WHERE enabled_at IS NOT NULL"))
        ).scalar_one()
        code_hashes = [
            row[0]
            for row in (
                await conn.execute(text("SELECT code_hash FROM user_mfa_recovery_codes WHERE used_at IS NULL"))
            ).all()
        ]
        return ciphertext, code_hashes

    ciphertext, code_hashes = run_db(read_stored_factor)
    assert secret.encode() not in ciphertext
    assert all(hashlib.sha256(code.replace("-", "").encode()).hexdigest() in code_hashes for code in recovery_codes)
    assert client.get("/api/auth/mfa/status").json() == {
        "enabled": True,
        "recovery_codes_remaining": 10,
        "required_by_organization": False,
    }
    client.post("/api/auth/logout")
    client.cookies.clear()
    return secret, recovery_codes


def test_password_login_requires_mfa_and_challenge_is_single_use(client, monkeypatch):
    secret, _ = _enable_mfa(client, monkeypatch)
    response = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.json()["mfa_required"] is True
    assert client.cookies.get("infraspine_session") is None
    challenge = response.json()["challenge_token"]
    stored_hash = run_db(lambda c: c.scalar(text("SELECT token_hash FROM user_mfa_challenges")))
    assert stored_hash == hashlib.sha256(challenge.encode()).hexdigest()
    assert stored_hash != challenge

    code = totp.totp_code(secret, int(_FIXED_TIME // totp.TOTP_PERIOD_SECONDS))
    wrong = client.post("/api/auth/mfa/challenge", json={"challenge_token": challenge, "code": "abcdef"})
    assert wrong.status_code == 401
    async def read_attempts(conn):
        return await conn.scalar(
            text("SELECT attempts FROM user_mfa_challenges WHERE token_hash = :h"), {"h": stored_hash}
        )

    assert run_db(read_attempts) == 1

    success = client.post("/api/auth/mfa/challenge", json={"challenge_token": challenge, "code": code})
    assert success.status_code == 200, success.text
    assert success.json()["email"] == ADMIN_EMAIL
    assert "infraspine_session" in client.cookies
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/mfa/challenge", json={"challenge_token": challenge, "code": code}).status_code == 401

    capped = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}).json()["challenge_token"]
    for attempt in range(5):
        response = client.post(
            "/api/auth/mfa/challenge", json={"challenge_token": capped, "code": "abcdef"}
        )
        assert response.status_code == (429 if attempt == 4 else 401)
    async def read_capped_challenge(conn):
        return (
            await conn.execute(
            text("SELECT attempts, consumed_at IS NOT NULL FROM user_mfa_challenges WHERE token_hash = :h"),
            {"h": hashlib.sha256(capped.encode()).hexdigest()},
            )
        ).one()

    capped_row = run_db(read_capped_challenge)
    assert tuple(capped_row) == (5, True)
    assert client.post(
        "/api/auth/mfa/challenge", json={"challenge_token": capped, "code": "abcdef"}
    ).status_code == 401
    assert client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}).status_code == 429


def test_recovery_code_is_single_use_and_can_disable_mfa(client, monkeypatch):
    secret, recovery_codes = _enable_mfa(client, monkeypatch)
    response = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    challenge = response.json()["challenge_token"]
    success = client.post(
        "/api/auth/mfa/challenge", json={"challenge_token": challenge, "code": recovery_codes[0]}
    )
    assert success.status_code == 200, success.text
    assert client.get("/api/auth/mfa/status").json()["recovery_codes_remaining"] == 9

    client.post("/api/auth/logout")
    client.cookies.clear()
    second_challenge = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}).json()["challenge_token"]
    reused = client.post(
        "/api/auth/mfa/challenge", json={"challenge_token": second_challenge, "code": recovery_codes[0]}
    )
    assert reused.status_code == 401

    # A password plus an unused recovery code authorizes a factor removal; the current session is retained.
    third_challenge = client.post(
        "/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    ).json()["challenge_token"]
    current_code = totp.totp_code(secret, int(_FIXED_TIME // totp.TOTP_PERIOD_SECONDS))
    assert client.post(
        "/api/auth/mfa/challenge", json={"challenge_token": third_challenge, "code": current_code}
    ).status_code == 200
    assert client.post(
        "/api/auth/mfa/disable", json={"password": PASSWORD, "code": recovery_codes[1]}
    ).status_code == 204
    assert client.get("/api/auth/mfa/status").json() == {
        "enabled": False,
        "recovery_codes_remaining": 0,
        "required_by_organization": False,
    }
    assert client.get("/api/auth/me").status_code == 200
