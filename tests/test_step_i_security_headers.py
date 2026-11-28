"""Step I slice 6: security headers, Teams Bot Framework JWT, status leak fix.

- Permissions-Policy is sent on every response; HSTS only when
  ``hsts_enabled`` is set (off by default — enabling it live is an owner
  decision, it must only run behind HTTPS).
- Teams ``POST /api/integrations/teams/messages`` accepts only Bot
  Framework bearer tokens for the configured app id (RS256, issuer
  ``https://api.botframework.com``); verified activities are only
  acknowledged, never processed. Missing/invalid tokens are 401.
- ``GET /api/integrations/status`` no longer reports whether a signing
  secret or an app id is configured (presence booleans were a deployment
  fact leak); it reports only whether each integration is enabled.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.security import teams_auth
from app.security.teams_auth import (
    BOT_FRAMEWORK_ISSUER,
    TeamsAuthError,
    authenticate_teams_request,
    verify_teams_bearer_token,
)

APP_ID = "test-teams-app-id-1234"


# --------------------------------------------------------------------------- helpers

_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-kid-1",
        "n": _b64url(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
    }
    return private, jwk


_PRIVATE_KEY, _JWK = _make_keypair()


def _provider():
    return {"test-kid-1": _PRIVATE_KEY.public_key()}


def _mint_token(
    private_key=_PRIVATE_KEY,
    *,
    kid="test-kid-1",
    app_id=APP_ID,
    issuer=BOT_FRAMEWORK_ISSUER,
    exp_offset=3600,
    alg="RS256",
    now=None,
):
    now = int(time.time()) if now is None else now
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    claims = {
        "iss": issuer,
        "aud": app_id,
        "exp": now + exp_offset,
        "nbf": now - 10,
        "iat": now,
    }
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    )
    if alg == "none":
        return signing_input + "."
    signature = private_key.sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return signing_input + "." + _b64url(signature)


# --------------------------------------------------------------------------- headers


def test_permissions_policy_present_and_hsts_off_by_default(monkeypatch):
    import app.main as main_module

    dev_settings = Settings(app_env="development")
    assert dev_settings.hsts_enabled is False
    monkeypatch.setattr(main_module, "get_settings", lambda: dev_settings)
    client = TestClient(main_module.create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["Permissions-Policy"] == _PERMISSIONS_POLICY
    assert "Strict-Transport-Security" not in resp.headers
    # The existing headers are untouched by the new ones.
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_hsts_present_when_enabled(monkeypatch):
    import app.main as main_module

    dev_settings = Settings(app_env="development", hsts_enabled=True)
    monkeypatch.setattr(main_module, "get_settings", lambda: dev_settings)
    client = TestClient(main_module.create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert resp.headers["Permissions-Policy"] == _PERMISSIONS_POLICY


# --------------------------------------------------------------------------- Teams JWT (pure)


def test_valid_teams_token_verifies():
    token = _mint_token()
    claims = verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)
    assert claims["aud"] == APP_ID
    assert claims["iss"] == BOT_FRAMEWORK_ISSUER


def test_teams_token_wrong_audience_rejected():
    token = _mint_token(app_id="some-other-app")
    with pytest.raises(TeamsAuthError, match="audience"):
        verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)


def test_teams_token_wrong_issuer_rejected():
    token = _mint_token(issuer="https://evil.example.com")
    with pytest.raises(TeamsAuthError, match="issuer"):
        verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)


def test_teams_token_expired_rejected():
    token = _mint_token(exp_offset=-10)
    with pytest.raises(TeamsAuthError, match="expired"):
        verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)


def test_teams_token_bad_signature_rejected():
    good = _mint_token()
    # Flip one character in the payload segment: the signature no longer matches.
    header, payload, signature = good.split(".")
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(TeamsAuthError, match="signature"):
        verify_teams_bearer_token(
            f"{header}.{tampered}.{signature}", app_id=APP_ID, key_provider=_provider
        )


def test_teams_token_unknown_kid_rejected():
    token = _mint_token(kid="unknown-kid")
    with pytest.raises(TeamsAuthError, match="unknown signing key"):
        verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)


def test_teams_token_none_algorithm_rejected():
    token = _mint_token(alg="none")
    with pytest.raises(TeamsAuthError, match="RS256"):
        verify_teams_bearer_token(token, app_id=APP_ID, key_provider=_provider)


def test_teams_token_malformed_rejected():
    for bad in ("", "not-a-jwt", "a.b", "a.b.c.d"):
        with pytest.raises(TeamsAuthError):
            verify_teams_bearer_token(bad, app_id=APP_ID, key_provider=_provider)


def test_teams_token_no_app_id_configured_rejected():
    token = _mint_token()
    with pytest.raises(TeamsAuthError, match="no Teams app id"):
        verify_teams_bearer_token(token, app_id="", key_provider=_provider)


def test_authenticate_teams_request_header_handling(monkeypatch):
    # The default key fetcher would hit the network; stub it to the test key.
    monkeypatch.setattr(teams_auth, "fetch_bot_framework_keys", lambda **kw: _provider())
    token = _mint_token()
    claims = authenticate_teams_request(f"Bearer {token}", app_id=APP_ID)
    assert claims["aud"] == APP_ID
    with pytest.raises(TeamsAuthError, match="missing Teams bearer token"):
        authenticate_teams_request(None, app_id=APP_ID)
    with pytest.raises(TeamsAuthError, match="missing Teams bearer token"):
        authenticate_teams_request("Basic abc", app_id=APP_ID)


# --------------------------------------------------------------------------- Teams route


def _teams_settings():
    return Settings(app_env="testing", teams_enabled=True, teams_app_id=APP_ID)


def test_teams_messages_verified_token_is_acknowledged_not_processed(monkeypatch):
    monkeypatch.setattr(teams_auth, "fetch_bot_framework_keys", lambda **kw: _provider())
    app.dependency_overrides[get_settings] = _teams_settings
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/integrations/teams/messages",
            json={"type": "message", "text": "What is the policy?"},
            headers={"Authorization": f"Bearer {_mint_token()}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "received", "answer_available": False}
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_teams_messages_invalid_or_missing_token_is_401(monkeypatch):
    monkeypatch.setattr(teams_auth, "fetch_bot_framework_keys", lambda **kw: _provider())
    app.dependency_overrides[get_settings] = _teams_settings
    try:
        client = TestClient(app)
        assert (
            client.post("/api/integrations/teams/messages", json={"type": "message"}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/integrations/teams/messages",
                json={"type": "message"},
                headers={"Authorization": f"Bearer {_mint_token(app_id='other-app')}"},
            ).status_code
            == 401
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_teams_messages_disabled_stays_503():
    # Default test settings have Teams disabled: the endpoint refuses
    # before any token is looked at.
    client = TestClient(app)
    resp = client.post(
        "/api/integrations/teams/messages",
        json={"type": "message"},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503


# --------------------------------------------------------------------------- status endpoint


def test_integration_status_reports_no_secret_presence():
    client = TestClient(app)
    resp = client.get("/api/integrations/status")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["slack"]) == {"enabled", "permission_scoped_answers_available"}
    assert set(data["teams"]) == {"enabled", "bot_framework_authentication_available"}
    assert data["teams"]["bot_framework_authentication_available"] is True
    assert "has_signing_secret" not in data["slack"]
    assert "has_app_id" not in data["teams"]
