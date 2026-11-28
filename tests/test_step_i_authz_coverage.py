"""Step I slice 1: authorization coverage of the HTTP API.

Proves every registered API route is either authenticated (session, partner
session, or SCIM bearer token in its FastAPI dependency tree) or is a
documented public / self-authenticating / signature-verified endpoint.
Any new unauthenticated route fails this test until it is added to the
allowlist with a reason — an allowlist entry is a deliberate, reviewable
decision, not a silent gap.

Evidence: runtime introspection of the real app (`create_app()`), which
registers 580 API routes on HEAD ca956c2 (2026-11-28). The app uses deferred
router inclusion (newer FastAPI `_IncludedRouter`), so the route walk
flattens those entries. Building the app needs no database: the lifespan
(which opens the engine) is never run here.
"""

import hashlib
import hmac
import time

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app
from app.services.collaboration import verify_slack_signature

# Dependency callables that authenticate a request. CSRF alone is NOT in this
# set: `verify_csrf` only proves a custom header is present, it does not
# identify the caller (login / callback routes authenticate themselves and are
# allowlisted below with their mechanism).
AUTHENTICATING_DEPENDENCIES = frozenset(
    {
        "get_current_user",
        "require_admin",
        "require_manager",
        "require_reports_viewer",
        "get_current_partner",
        "verify_scim_token",
    }
)

# (method, path) -> why no session is needed. Keep this exact: the test
# fails on any route outside the guarded set and this allowlist, and it also
# fails on stale allowlist entries that no longer exist in the app.
ALLOWLIST = {
    # Public by design — no data, no secrets.
    ("GET", "/"): "Admin UI redirect (or 404 when the UI is not built).",
    ("GET", "/favicon.ico"): "Static 204.",
    ("GET", "/health"): "Liveness probe.",
    ("GET", "/health/ready"): "Readiness probe.",
    ("GET", "/api/auth/sso/config"): "Public SSO discovery config (enabled flag, provider type, authorization URL — no secrets).",
    ("GET", "/api/deployment/profile"): "Pre-sign-in branding only; explicitly no customer records, license data or secrets.",
    ("GET", "/api/scim/v2/Schemas"): "SCIM discovery metadata (spec-normal public).",
    ("GET", "/api/scim/v2/ServiceProviderConfig"): "SCIM discovery metadata (spec-normal public).",
    # Authenticate themselves — credentials / tokens, not a session cookie.
    ("POST", "/api/auth/login"): "Authenticates with email + password (Argon2id) + optional MFA; login lockout applies.",
    ("POST", "/api/auth/mfa/challenge"): "Authenticates with the MFA challenge (TOTP).",
    ("POST", "/api/auth/sso/callback"): "Authenticates by exchanging an OIDC code / ID token with the provider.",
    ("POST", "/api/auth/sso/saml"): "Authenticates with a signed SAML 2.0 assertion.",
    ("POST", "/api/onboarding/accept"): "Authenticates with a single-use invitation token + new password.",
    ("POST", "/api/partner/auth/accept"): "Authenticates with a single-use partner invitation token + password.",
    ("POST", "/api/partner/auth/login"): "Authenticates with email + workspace + password (partner session).",
    # Signature-verified webhooks — the provider's HMAC, not a session.
    ("GET", "/api/agent-studio/whatsapp/webhook/{connection_id}"): "Meta webhook verification via per-connection verify token.",
    ("POST", "/api/agent-studio/whatsapp/webhook/{connection_id}"): "Meta webhook events via X-Hub-Signature-256 HMAC.",
    ("POST", "/api/integrations/slack/events"): "Slack events via X-Slack-Signature HMAC + 5-minute replay window.",
    ("POST", "/api/integrations/slack/command"): "Slack slash commands via X-Slack-Signature HMAC + 5-minute replay window.",
    ("POST", "/api/integrations/webhooks/{connector_id}"): "Connector events via X-InfraSpine-Signature HMAC.",
    # Public status flags — presence booleans only, no secret values.
    ("GET", "/api/integrations/status"): "Reports only whether Slack/Teams are enabled (booleans).",
    # Refuses all activity.
    ("POST", "/api/integrations/teams/messages"): "Bot Framework bearer-token authentication (RS256, issuer api.botframework.com, audience teams_app_id); verified activities are only acknowledged, never processed.",
}


def _iter_api_routes(routes, prefix=""):
    """Yield (path, APIRoute), flattening deferred router inclusions."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif getattr(route, "original_router", None) is not None:
            context = getattr(route, "include_context", None)
            included_prefix = (getattr(context, "prefix", "") or "") if context is not None else ""
            yield from _iter_api_routes(route.original_router.routes, prefix + included_prefix)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes, prefix + (getattr(route, "path", "") or ""))


def _dependency_names(route):
    """All dependency callable names in the route's FastAPI dependency tree."""
    names = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            names.add(getattr(sub.call, "__name__", str(sub.call)))
            walk(sub)

    walk(route.dependant)
    return names


def _all_routes():
    return list(_iter_api_routes(app.routes))


def test_every_route_is_authenticated_or_documented():
    offenders = []
    for path, route in _all_routes():
        names = _dependency_names(route)
        if names & AUTHENTICATING_DEPENDENCIES:
            continue
        for method in sorted(route.methods):
            if (method, path) not in ALLOWLIST:
                offenders.append(f"{method} {path} (deps: {sorted(names) or ['none']})")
    assert not offenders, (
        "Unauthenticated routes with no documented reason:\n" + "\n".join(offenders)
        + "\nAdd the route to ALLOWLIST with its authentication mechanism, "
        "or add a session/token dependency."
    )


def test_allowlist_entries_exist_in_the_app():
    present = {(method, path) for path, route in _all_routes() for method in route.methods}
    stale = sorted(key for key in ALLOWLIST if key not in present)
    assert not stale, f"Allowlist entries no longer in the app (remove them): {stale}"
    # Guard against silent shrinkage of the inventory: the app is expected to
    # keep roughly this many routes; a big drop means the walk broke.
    assert len(present) >= 500, f"Only {len(present)} routes found; the route walk is broken?"


def test_login_requires_csrf_dependency():
    for path, route in _all_routes():
        if path == "/api/auth/login" and "POST" in route.methods:
            assert "verify_csrf" in _dependency_names(route), (
                "POST /api/auth/login must require the CSRF header (it takes credentials)."
            )
            return
    pytest.fail("POST /api/auth/login not found in the app")


# --- Slack webhook signature verification (pure function, no DB) ------------


def _signed(secret, body, timestamp):
    base = f"v0:{timestamp}:".encode("ascii") + body
    return "v0=" + hmac.new(secret.encode("ascii"), base, hashlib.sha256).hexdigest()


def test_slack_signature_accepts_valid_request():
    secret = "test-signing-secret"
    body = b'{"type": "event_callback"}'
    timestamp = str(int(time.time()))
    assert verify_slack_signature(body, timestamp, _signed(secret, body, timestamp), secret) is True


def test_slack_signature_rejects_tampered_body():
    secret = "test-signing-secret"
    body = b'{"type": "event_callback"}'
    timestamp = str(int(time.time()))
    signature = _signed(secret, body, timestamp)
    assert verify_slack_signature(b'{"type": "url_verification"}', timestamp, signature, secret) is False


def test_slack_signature_rejects_wrong_secret():
    body = b"hello"
    timestamp = str(int(time.time()))
    signature = _signed("correct-secret", body, timestamp)
    assert verify_slack_signature(body, timestamp, signature, "wrong-secret") is False


def test_slack_signature_rejects_stale_timestamp():
    secret = "test-signing-secret"
    body = b"hello"
    timestamp = str(int(time.time()) - 3600)  # one hour old: replay window is 5 minutes
    assert verify_slack_signature(body, timestamp, _signed(secret, body, timestamp), secret) is False


def test_slack_signature_rejects_malformed_or_missing_inputs():
    body = b"hello"
    timestamp = str(int(time.time()))
    assert verify_slack_signature(body, timestamp, "not-a-signature", "secret") is False
    assert verify_slack_signature(body, timestamp, "", "secret") is False
    assert verify_slack_signature(body, "", _signed("secret", body, timestamp), "secret") is False
    assert verify_slack_signature(body, timestamp, _signed("secret", body, timestamp), "") is False
    assert verify_slack_signature(body, "not-a-timestamp", _signed("secret", body, timestamp), "secret") is False


# --- Session cookie guard ----------------------------------------------------


def test_production_rejects_insecure_session_cookie():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://stepi:stepi@127.0.0.1:5432/stepi",
            master_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            app_env="production",
            session_cookie_secure=False,
        )
