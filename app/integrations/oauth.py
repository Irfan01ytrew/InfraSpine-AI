"""Authorization Code + PKCE; one-use state bound to owner, tenant and version."""
import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select

from app.core.errors import ConflictError, InvalidInputError
from app.integrations import transport
from app.integrations.models import OAuthAttempt
from app.integrations.security import seal, unseal, scope


def endpoints(connector):
    if connector.provider == "microsoft365":
        tenant = connector.config["microsoft_tenant"]
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        return base + "/authorize", base, "/token"
    if connector.provider in ("gmail", "google_calendar", "google_drive"):
        return "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com", "/token"
    raise InvalidInputError("OAuth authorization is supported here for Microsoft 365 and Google services.")


async def begin(service, tenant_id, connector_id):
    row = await service.connector(tenant_id, connector_id, write=True)
    if not row.enabled or not row.credentials: raise InvalidInputError("Configure this connector first.")
    authorization_url, _, _ = endpoints(row)
    secret = unseal(service.cipher, "credentials", row, row.credentials)
    redirect = row.config["redirect_uri"]
    parsed = urlsplit(redirect)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment or not parsed.path.endswith("/admin/"):
        raise InvalidInputError("Set an HTTPS application /admin/ redirect URI registered with the provider.")
    if not secret.get("client_id") or not row.config["scopes"]:
        raise InvalidInputError("Configure the registered OAuth client and requested scopes.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    attempt = OAuthAttempt(id=uuid.uuid4(), tenant_id=tenant_id, owner_id=service.user.id, connector_id=row.id,
                           state_hash=hashlib.sha256(state.encode()).hexdigest(), connector_version=row.version,
                           expires_at=datetime.now(UTC) + timedelta(minutes=10), used=False)
    attempt.verifier = seal(service.cipher, "oauth", attempt, {"verifier": verifier})
    service.db.add(attempt)
    params = {"response_type": "code", "client_id": secret["client_id"], "redirect_uri": redirect,
              "scope": " ".join(row.config["scopes"]), "state": state, "code_challenge_method": "S256",
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if row.provider != "microsoft365": params.update(access_type="offline", prompt="consent")
    service.record("oauth_started", row)
    await service.db.commit()
    return {"authorization_url": authorization_url + "?" + urlencode(params)}


async def exchange(service, tenant_id, connector_id, state, code):
    row = await service.connector(tenant_id, connector_id, write=True)
    attempt = await service.db.scalar(select(OAuthAttempt).where(*scope(OAuthAttempt, tenant_id, service.user.id),
        OAuthAttempt.connector_id == row.id, OAuthAttempt.state_hash == hashlib.sha256(state.encode()).hexdigest()).with_for_update())
    if not attempt or attempt.used or attempt.expires_at <= datetime.now(UTC) or attempt.connector_version != row.version or not row.enabled:
        raise ConflictError("OAuth state is invalid, expired, used or belongs to an older configuration.")
    secret = unseal(service.cipher, "credentials", row, row.credentials)
    verifier = unseal(service.cipher, "oauth", attempt, attempt.verifier)["verifier"]
    _, base, path = endpoints(row)
    # Consume before the remote exchange: an ambiguous exchange requires a new authorization.
    attempt.used = True
    await service.db.commit()
    row = await service.connector(tenant_id, connector_id, write=True)
    if row.version != attempt.connector_version or not row.enabled: raise ConflictError()
    form = {"grant_type": "authorization_code", "code": code, "client_id": secret["client_id"],
            "code_verifier": verifier, "redirect_uri": row.config["redirect_uri"]}
    if secret.get("client_secret"): form["client_secret"] = secret["client_secret"]
    response = await transport.request(base, path, method="POST", form=form, write=True)
    apply_token(secret, response.body, row.config["scopes"])
    row.credentials = seal(service.cipher, "credentials", row, secret)
    row.health = "unverified"; row.version += 1; row.verified_at = None
    service.record("oauth_connected", row, version=row.version)
    await service.db.commit()
    return {"connected": True, "verified": False}


def apply_token(secret, body, requested_scopes):
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not 1 <= len(body["access_token"]) <= 10000:
        raise transport.TransportError("invalid_token_response")
    if body.get("token_type", "Bearer").lower() != "bearer":
        raise transport.TransportError("unsupported_token_type")
    if body.get("scope") is not None and not set(requested_scopes) - {"offline_access"} <= set(body["scope"].split()):
        raise transport.TransportError("required_scope_not_granted")
    try:
        expires = int(body.get("expires_in", 3600))
        if not 1 <= expires <= 86400: raise ValueError()
    except (TypeError, ValueError):
        raise transport.TransportError("invalid_token_expiry") from None
    secret["access_token"] = body["access_token"]
    if body.get("refresh_token"): secret["refresh_token"] = body["refresh_token"]
    secret["expires_at"] = (datetime.now(UTC) + timedelta(seconds=expires)).isoformat()


async def usable_secret(db, cipher, row):
    secret = unseal(cipher, "credentials", row, row.credentials)
    expires = datetime.fromisoformat(secret["expires_at"]) if secret.get("expires_at") else None
    if expires is None or expires > datetime.now(UTC) + timedelta(seconds=60): return secret
    if not secret.get("refresh_token") or not secret.get("client_id"):
        raise transport.TransportError("token_expired")
    _, base, path = endpoints(row)
    form = {"grant_type": "refresh_token", "refresh_token": secret["refresh_token"], "client_id": secret["client_id"]}
    if secret.get("client_secret"): form["client_secret"] = secret["client_secret"]
    response = await transport.request(base, path, method="POST", form=form)
    apply_token(secret, response.body, row.config["scopes"])
    row.credentials = seal(cipher, "credentials", row, secret)
    # Caller retains the connector lock and commits this with the dispatch marker/result.
    return secret
