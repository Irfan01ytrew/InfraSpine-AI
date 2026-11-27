"""Enterprise SSO (OIDC & SAML 2.0) authentication routes."""

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import AppSettings, DbSession, audit_context, verify_csrf
from app.core.security import SESSION_COOKIE_NAME
from app.schemas.auth import UserResponse
from app.schemas.sso import SsoCallbackRequest, SsoConfigResponse, SsoSamlAssertionRequest
from app.services import sso

router = APIRouter(prefix="/api/auth/sso", tags=["sso"])


@router.get("/config", response_model=SsoConfigResponse)
async def get_sso_config(settings: AppSettings) -> SsoConfigResponse:
    """Return SSO configuration and login URL for frontend redirect."""
    auth_url = sso.get_sso_authorization_url(settings) if settings.sso_enabled else None
    return SsoConfigResponse(
        enabled=settings.sso_enabled,
        provider_type=settings.sso_provider_type,
        authorization_url=auth_url,
    )


@router.post("/callback", response_model=UserResponse, dependencies=[Depends(verify_csrf)])
async def sso_callback(
    payload: SsoCallbackRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> UserResponse:
    """Handle OIDC authorization code or ID token callback."""
    user, session_token = await sso.process_oidc_callback(
        code=payload.code,
        db=db,
        settings=settings,
        id_token=payload.id_token,
        audit_ctx=audit_context(request, None),
    )

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    return UserResponse(id=user.id, email=user.email, role=user.role)


@router.post("/saml", response_model=UserResponse, dependencies=[Depends(verify_csrf)])
async def sso_saml_assertion(
    payload: SsoSamlAssertionRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> UserResponse:
    """Handle SAML 2.0 assertion submission."""
    user, session_token = await sso.process_saml_assertion(
        saml_response_b64=payload.saml_response,
        db=db,
        settings=settings,
        audit_ctx=audit_context(request, None),
    )

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    return UserResponse(id=user.id, email=user.email, role=user.role)
