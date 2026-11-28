"""Microsoft Teams Bot Framework JWT validation (Step I slice 6).

Bot Framework activities arrive with an ``Authorization: Bearer`` token
issued by Microsoft for this bot's app id. We verify that token before
doing anything with the activity:

- RS256 signature against the Bot Framework OpenID signing keys
  (``login.botframework.com`` JWKS, cached for one hour),
- issuer exactly ``https://api.botframework.com``,
- audience exactly the configured ``teams_app_id``,
- expiry (and ``nbf`` when present).

Verification proves the caller is the Bot Framework service acting for
this app id — a token minted for any other app id, or by anyone else,
does not verify. The activity body itself is still untrusted data: after
verification the endpoint only acknowledges receipt (``answer_available:
False``), it never acts on the activity, never calls RAG, and never
creates an approval.

Only ``cryptography`` + stdlib are used (no PyJWT in the lockfile).
Network access happens only in the default key fetcher; tests inject a
``key_provider`` returning ``{kid: RSAPublicKey}`` and never touch the
network.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import urllib.request
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_KEY_CACHE_TTL_SECONDS = 3600

KeyProvider = Callable[[], dict[str, rsa.RSAPublicKey]]


class TeamsAuthError(Exception):
    """A Teams bearer token failed authentication."""


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise TeamsAuthError("malformed base64url segment") from exc


def _public_key_from_jwk(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    """Build an RSA public key from a JWKS entry (``kty == "RSA"``)."""
    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    except (KeyError, TeamsAuthError) as exc:
        raise TeamsAuthError("JWKS entry is missing its RSA parameters") from exc
    return rsa.RSAPublicNumbers(e, n).public_key()


_keys_cache: dict[str, Any] = {"keys": None, "expires_at": 0.0}


def fetch_bot_framework_keys(*, refresh: bool = False) -> dict[str, rsa.RSAPublicKey]:
    """Fetch the Bot Framework signing keys (JWKS), cached for one hour."""
    now = time.time()
    if not refresh and _keys_cache["keys"] is not None and now < _keys_cache["expires_at"]:
        return _keys_cache["keys"]
    try:
        with urllib.request.urlopen(OPENID_CONFIG_URL, timeout=10) as resp:
            config = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen(config["jwks_uri"], timeout=10) as resp:
            jwks = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network down, bad JSON, missing jwks_uri
        raise TeamsAuthError("could not fetch Bot Framework signing keys") from exc
    keys: dict[str, rsa.RSAPublicKey] = {}
    for entry in jwks.get("keys", []):
        kid = entry.get("kid")
        if entry.get("kty") == "RSA" and kid and "n" in entry and "e" in entry:
            try:
                keys[kid] = _public_key_from_jwk(entry)
            except TeamsAuthError:
                continue
    _keys_cache["keys"] = keys
    _keys_cache["expires_at"] = now + _KEY_CACHE_TTL_SECONDS
    return keys


def verify_teams_bearer_token(
    token: str,
    *,
    app_id: str,
    key_provider: KeyProvider | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a Bot Framework bearer token; return its claims.

    Raises :class:`TeamsAuthError` on any failure. ``key_provider`` returns
    ``{kid: RSAPublicKey}`` and defaults to the cached Bot Framework JWKS
    fetcher. ``now`` (seconds since epoch) is injectable for tests.
    """
    if not app_id:
        raise TeamsAuthError("no Teams app id configured")
    if not isinstance(token, str) or not token:
        raise TeamsAuthError("missing Teams bearer token")
    parts = token.split(".")
    if len(parts) != 3:
        raise TeamsAuthError("bearer token is not a JWT")
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
    except TeamsAuthError as exc:
        raise TeamsAuthError("JWT header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise TeamsAuthError("JWT header is not a JSON object")
    if header.get("alg") != "RS256":
        raise TeamsAuthError("JWT is not RS256-signed")
    kid = header.get("kid")
    if not kid:
        raise TeamsAuthError("JWT header has no key id")

    provider = key_provider or fetch_bot_framework_keys
    keys = provider()
    key = keys.get(kid)
    if key is None and key_provider is None:
        # Keys rotate; a single refresh covers a kid published after our cache.
        keys = fetch_bot_framework_keys(refresh=True)
        key = keys.get(kid)
    if key is None:
        raise TeamsAuthError("unknown signing key")

    signature = _b64url_decode(signature_segment)
    signed = f"{header_segment}.{payload_segment}".encode("ascii")
    try:
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise TeamsAuthError("JWT signature does not verify") from exc

    # Only trust the claims after the signature has verified.
    try:
        claims = json.loads(_b64url_decode(payload_segment))
    except TeamsAuthError as exc:
        raise TeamsAuthError("JWT payload is not valid JSON") from exc
    if not isinstance(claims, dict):
        raise TeamsAuthError("JWT payload is not a JSON object")
    if claims.get("iss") != BOT_FRAMEWORK_ISSUER:
        raise TeamsAuthError("JWT issuer is not the Bot Framework")
    if claims.get("aud") != app_id:
        raise TeamsAuthError("JWT audience is not this Teams app id")
    current = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp <= current:
        raise TeamsAuthError("JWT is expired or has no expiry")
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or nbf > current):
        raise TeamsAuthError("JWT is not yet valid")
    return claims


def authenticate_teams_request(
    authorization: str | None,
    *,
    app_id: str,
    key_provider: KeyProvider | None = None,
) -> dict[str, Any]:
    """Extract and verify the Bearer token from an ``Authorization`` header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise TeamsAuthError("missing Teams bearer token")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise TeamsAuthError("missing Teams bearer token")
    return verify_teams_bearer_token(token, app_id=app_id, key_provider=key_provider)
