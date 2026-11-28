"""API rate limiting (Step I slice 2).

Per-process, in-memory fixed-window limits. Two layers:

* Per-IP buckets enforced by the HTTP middleware in app/main.py on /api/
  paths (``auth`` for sign-in endpoints, ``webhook`` for signature-verified
  webhooks, ``api`` for everything else). Health/readiness endpoints are
  never limited, so probes and load-balancer checks cannot be locked out.
* Per-user bucket enforced in ``get_current_user`` (app/api/deps.py) for
  authenticated callers. Administrators are exempt: the owner admin can
  never be locked out of their own deployment.

Limiting is off entirely when ``APP_ENV=testing`` (or
``rate_limit_enabled=false``) so test suites are never throttled; slice
tests enable it explicitly by building an app with APP_ENV=development.

The store is per-process: in a multi-worker deployment each worker keeps
its own counters. That is a deliberate v1 limitation (documented in the
Step I plan): the limits still bound floods per worker, and the per-user
bucket is enforced on every authenticated request.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import RateLimitError
from app.db.models import ROLE_ADMIN

EXEMPT_PATHS = frozenset({"/", "/health", "/health/ready", "/favicon.ico"})
EXEMPT_PREFIXES = ("/admin", "/docs", "/openapi.json")

_AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/mfa/challenge", "/api/partner/auth/login"})


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0


class RateLimiter:
    """Thread-safe fixed-window counter store. Keys are opaque strings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, limit: int, window_seconds: int, *, now: float | None = None) -> RateLimitResult:
        """Record one hit against ``key``. ``limit <= 0`` means unlimited."""
        if limit <= 0:
            return RateLimitResult(True)
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= window_seconds:
                start, count = now, 0
            count += 1
            self._windows[key] = (start, count)
            if count > limit:
                retry = int(window_seconds - (now - start)) + 1
                return RateLimitResult(False, max(1, retry))
            return RateLimitResult(True)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


def classify_route(method: str, path: str) -> str:
    """Bucket name for a request. Sign-in endpoints get the strictest bucket
    (they front Argon2id hashing, so floods are expensive); webhooks get
    their own bucket so a provider flood cannot eat the interactive budget."""
    if path in _AUTH_PATHS:
        return "auth"
    if "/webhook" in path or path.startswith("/api/integrations/slack/"):
        return "webhook"
    return "api"


def is_exempt_path(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def _enabled(settings: Any) -> bool:
    return bool(getattr(settings, "rate_limit_enabled", True)) and getattr(
        settings, "app_env", "development") != "testing"


def _bucket_limit(settings: Any, bucket: str) -> tuple[int, int]:
    if bucket == "auth":
        return int(getattr(settings, "rate_limit_auth_per_ip_per_minute", 30)), 60
    if bucket == "webhook":
        return int(getattr(settings, "rate_limit_webhook_per_ip_per_minute", 120)), 60
    return int(getattr(settings, "rate_limit_per_ip_per_minute", 600)), 60


def rate_limit_response(request: Request, settings: Any) -> JSONResponse | None:
    """Middleware hook: None when the request may proceed, else a 429 response
    with a Retry-After header. Fails open when no limiter is configured."""
    if not _enabled(settings):
        return None
    path = request.url.path
    if not path.startswith("/api/") or is_exempt_path(path):
        return None
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return None
    bucket = classify_route(request.method, path)
    limit, window = _bucket_limit(settings, bucket)
    ip = request.client.host if request.client else "unknown"
    result = limiter.allow(f"{bucket}:{ip}", limit, window)
    if result.allowed:
        return None
    return JSONResponse(
        status_code=429,
        content={"error": {"code": "rate_limited", "message": "Too many requests. Please try again shortly."}},
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


def check_user_rate_limit(request: Request, user: Any, settings: Any) -> None:
    """Per-user bucket for authenticated callers. Raises RateLimitError.

    Administrators are exempt. Fails open when no limiter is configured on
    the app (e.g. a dependency invoked outside a request served by the app).
    """
    if not _enabled(settings):
        return
    if getattr(user, "role", None) == ROLE_ADMIN:
        return
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    limit = int(getattr(settings, "rate_limit_per_user_per_minute", 600))
    result = limiter.allow(f"user:{user.id}", limit, 60)
    if not result.allowed:
        raise RateLimitError()
