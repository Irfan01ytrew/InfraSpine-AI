"""Step I slice 2: API rate limiting.

Pure-limiter unit tests, middleware tests against a fresh app built with
APP_ENV=development and tiny limits, and a per-user wiring test through the
real dependency (dependency_overrides on the module app, throwaway test DB).

Rate limiting is off when APP_ENV=testing, so the rest of the suite is
never throttled; these tests opt in explicitly.
"""

import types

import pytest
from fastapi.testclient import TestClient

from app.core.errors import RateLimitError
from app.security.rate_limit import (
    RateLimiter,
    check_user_rate_limit,
    classify_route,
    is_exempt_path,
    rate_limit_response,
)


# --------------------------------------------------------------------------- pure limiter


def test_limiter_allows_up_to_limit_then_denies():
    limiter = RateLimiter()
    assert all(limiter.allow("k", 3, 60, now=1000.0).allowed for _ in range(3))
    denied = limiter.allow("k", 3, 60, now=1000.0)
    assert not denied.allowed
    assert denied.retry_after_seconds >= 1


def test_limiter_window_resets_after_expiry():
    limiter = RateLimiter()
    for _ in range(3):
        assert limiter.allow("k", 3, 60, now=1000.0).allowed
    assert not limiter.allow("k", 3, 60, now=1000.0).allowed
    assert limiter.allow("k", 3, 60, now=1061.0).allowed  # new window


def test_limiter_keys_are_independent():
    limiter = RateLimiter()
    for _ in range(3):
        assert limiter.allow("a", 3, 60, now=1000.0).allowed
    assert not limiter.allow("a", 3, 60, now=1000.0).allowed
    assert limiter.allow("b", 3, 60, now=1000.0).allowed


def test_limiter_zero_limit_means_unlimited():
    limiter = RateLimiter()
    for _ in range(50):
        assert limiter.allow("k", 0, 60, now=1000.0).allowed


def test_limiter_reset_clears_windows():
    limiter = RateLimiter()
    for _ in range(3):
        limiter.allow("k", 3, 60, now=1000.0)
    limiter.reset()
    assert limiter.allow("k", 3, 60, now=1000.0).allowed


def test_classify_route_buckets():
    assert classify_route("POST", "/api/auth/login") == "auth"
    assert classify_route("POST", "/api/auth/mfa/challenge") == "auth"
    assert classify_route("POST", "/api/partner/auth/login") == "auth"
    assert classify_route("POST", "/api/agent-studio/whatsapp/webhook/abc") == "webhook"
    assert classify_route("POST", "/api/integrations/slack/events") == "webhook"
    assert classify_route("GET", "/api/finance/scopes") == "api"
    assert classify_route("GET", "/api/auth/me") == "api"


def test_is_exempt_path():
    for path in ("/health", "/health/ready", "/", "/favicon.ico", "/admin/", "/docs"):
        assert is_exempt_path(path), path
    assert not is_exempt_path("/api/auth/login")
    assert not is_exempt_path("/api/finance/scopes")


# --------------------------------------------------------------------------- middleware hook (unit level)


def _stub_request(method="GET", path="/api/finance/scopes"):
    request = types.SimpleNamespace()
    request.method = method
    request.url = types.SimpleNamespace(path=path)
    request.client = types.SimpleNamespace(host="10.0.0.1")
    request.app = types.SimpleNamespace(state=types.SimpleNamespace(rate_limiter=RateLimiter()))
    return request


def _dev_settings(**overrides):
    values = {
        "rate_limit_enabled": True,
        "rate_limit_per_ip_per_minute": 600,
        "rate_limit_per_user_per_minute": 600,
        "rate_limit_auth_per_ip_per_minute": 30,
        "rate_limit_webhook_per_ip_per_minute": 120,
    }
    values.update(overrides)
    return types.SimpleNamespace(app_env="development", **values)


def test_rate_limit_response_returns_none_when_disabled():
    request = _stub_request()
    settings = _dev_settings(rate_limit_enabled=False)
    assert rate_limit_response(request, settings) is None


def test_rate_limit_response_returns_none_in_testing_env():
    request = _stub_request()
    settings = _dev_settings()
    settings.app_env = "testing"
    assert rate_limit_response(request, settings) is None


def test_rate_limit_response_exempts_health():
    request = _stub_request(path="/health")
    assert rate_limit_response(request, _dev_settings()) is None


def test_rate_limit_response_throttles_after_limit():
    request = _stub_request()
    settings = _dev_settings(rate_limit_per_ip_per_minute=2)
    assert rate_limit_response(request, settings) is None
    assert rate_limit_response(request, settings) is None
    limited = rate_limit_response(request, settings)
    assert limited is not None
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    assert limited.body is not None


def test_rate_limit_response_fails_open_without_limiter():
    request = _stub_request()
    request.app.state.rate_limiter = None
    assert rate_limit_response(request, _dev_settings()) is None


# --------------------------------------------------------------------------- per-user helper


def _stub_user(role="employee"):
    return types.SimpleNamespace(id="00000000-0000-0000-0000-000000000001", role=role)


def test_check_user_rate_limit_denies_after_limit():
    request = _stub_request()
    settings = _dev_settings(rate_limit_per_user_per_minute=2)
    user = _stub_user()
    check_user_rate_limit(request, user, settings)
    check_user_rate_limit(request, user, settings)
    with pytest.raises(RateLimitError):
        check_user_rate_limit(request, user, settings)


def test_check_user_rate_limit_never_limits_admins():
    request = _stub_request()
    settings = _dev_settings(rate_limit_per_user_per_minute=1)
    admin = _stub_user(role="admin")
    for _ in range(5):
        check_user_rate_limit(request, admin, settings)


def test_check_user_rate_limit_off_in_testing_env():
    request = _stub_request()
    settings = _dev_settings(rate_limit_per_user_per_minute=1)
    settings.app_env = "testing"
    for _ in range(5):
        check_user_rate_limit(request, _stub_user(), settings)


# --------------------------------------------------------------------------- middleware through a real app


def test_middleware_returns_429_and_health_stays_open(monkeypatch):
    # Do NOT touch the environment or the cached get_settings(): the
    # middleware test must not poison the settings cache for later tests
    # (an earlier version cleared the cache while DATABASE_URL was
    # monkeypatched to a dummy host, and the next test's login then
    # connected to 127.0.0.1:1). Instead build the dev settings object
    # directly and patch only app.main's get_settings for create_app().
    from app.core.config import Settings
    import app.main as main_module

    dev_settings = Settings(
        app_env="development",
        rate_limit_per_ip_per_minute=3,
        rate_limit_auth_per_ip_per_minute=3,
        rate_limit_webhook_per_ip_per_minute=3,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: dev_settings)
    app2 = main_module.create_app()
    client = TestClient(app2)
    statuses = [client.get("/api/nope").status_code for _ in range(5)]
    assert statuses[:3] == [404, 404, 404]
    assert statuses[3] == 429
    assert statuses[4] == 429
    limited = client.get("/api/nope")
    assert limited.headers.get("Retry-After", "").isdigit()
    assert limited.json()["error"]["code"] == "rate_limited"
    # Health/readiness are never limited, even after the budget is spent.
    assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


# --------------------------------------------------------------------------- per-user wiring through get_current_user


def test_per_user_limit_wired_and_admin_exempt(client, monkeypatch):
    from app.core.config import Settings, get_settings
    from app.main import app as main_app
    from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("RATE_LIMIT_PER_USER_PER_MINUTE", "3")
    settings = Settings()
    assert settings.app_env == "development"
    main_app.dependency_overrides[get_settings] = lambda: settings
    limiter = main_app.state.rate_limiter
    limiter.reset()
    try:
        login(client, USER_EMAIL)
        codes = [client.get("/api/auth/me").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429
        assert codes[4] == 429
        # The owner admin is never per-user limited.
        client.cookies.clear()
        login(client, ADMIN_EMAIL)
        assert [client.get("/api/auth/me").status_code for _ in range(5)] == [200] * 5
    finally:
        main_app.dependency_overrides.pop(get_settings, None)
        limiter.reset()
