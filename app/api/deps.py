"""FastAPI dependencies shared across routes.

FastAPI caches dependencies per request, so every dependency in one request
shares the same database session.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationRequiredError,
    CsrfRejectedError,
    MfaEnrollmentRequiredError,
    MfaReauthenticationRequiredError,
    PermissionDeniedError,
)
from app.core.security import CSRF_HEADER_NAME, PARTNER_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from app.db.models import ROLE_ADMIN, ROLE_MANAGER, User
from app.services import auth
from app.services.audit import AuditContext
from app.services.llm_runtime import LLMRuntime
from app.services.provider_admin import ProviderAdminService
from app.services.partner_portal import PartnerPrincipal, resolve_session as resolve_partner_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MFA_ENROLLMENT_PATHS = {
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/mfa/status",
    "/api/auth/mfa/enrollment",
    "/api/auth/mfa/enrollment/confirm",
}
_MFA_REAUTHENTICATION_PATHS = {
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/mfa/status",
}


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def verify_csrf(request: Request) -> None:
    """Require a custom header on state-changing requests.

    Browsers cannot attach custom headers to cross-site requests without a CORS
    preflight, and this API grants no CORS permissions, so a forged cross-site
    form or fetch cannot pass. Combined with SameSite=Strict session cookies.
    """
    if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER_NAME):
        raise CsrfRejectedError()


def audit_context(request: Request, user: User | None) -> AuditContext:
    return AuditContext(
        actor_user_id=user.id if user else None,
        actor_email=user.email if user else None,
        ip_address=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


async def get_current_user(request: Request, db: DbSession, settings: AppSettings) -> User:
    verify_csrf(request)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise AuthenticationRequiredError()
    user = await auth.resolve_session(db, token)
    if user is None:
        raise AuthenticationRequiredError()
    if settings.org_mfa_required:
        enrollment_required, reauthentication_required = await auth.mfa_policy_state(db, user, token, settings)
        if enrollment_required and request.url.path not in _MFA_ENROLLMENT_PATHS:
            raise MfaEnrollmentRequiredError()
        if reauthentication_required and request.url.path not in _MFA_REAUTHENTICATION_PATHS:
            raise MfaReauthenticationRequiredError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_partner(request: Request, db: DbSession) -> PartnerPrincipal:
    """Resolve only the partner-scoped cookie; employee sessions never authenticate this route."""
    verify_csrf(request)
    token = request.cookies.get(PARTNER_SESSION_COOKIE_NAME)
    if not token:
        raise AuthenticationRequiredError()
    principal = await resolve_partner_session(db, token)
    if principal is None:
        raise AuthenticationRequiredError()
    return principal


CurrentPartner = Annotated[PartnerPrincipal, Depends(get_current_partner)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != ROLE_ADMIN:
        raise PermissionDeniedError()
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def require_reports_viewer(user: CurrentUser, db: DbSession) -> User:
    """Administrators, or anyone holding an active (not expired or revoked) reports.view grant."""
    if user.role == ROLE_ADMIN:
        return user
    from app.core.permission_catalog import PERMISSION_REPORTS_VIEW
    from app.services.access_control import find_active_grant

    if await find_active_grant(db, user.id, PERMISSION_REPORTS_VIEW, "general", None) is None:
        raise PermissionDeniedError("Viewing organization reports requires the reports.view permission.")
    return user


async def require_manager(user: CurrentUser) -> User:
    if user.role != ROLE_MANAGER:
        raise PermissionDeniedError("Department manager access is required.")
    return user


ManagerUser = Annotated[User, Depends(require_manager)]


async def get_provider_admin(
    request: Request, db: DbSession, settings: AppSettings, admin: AdminUser
) -> ProviderAdminService:
    return ProviderAdminService(db, settings, request.app.state.cipher, audit_context(request, admin))


def get_llm_runtime(request: Request) -> LLMRuntime:
    """The per-process runtime; call `get_client(db)` after input validation has passed."""
    return request.app.state.llm_runtime
