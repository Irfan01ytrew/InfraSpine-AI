from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, verify_csrf
from app.core.errors import PermissionDeniedError
from app.core.security import SESSION_COOKIE_NAME
from app.schemas.auth import (
    LoginRequest,
    MfaChallengeRequest,
    MfaChallengeResponse,
    MfaCodeRequest,
    MfaEnrollmentResponse,
    MfaEnrollmentStartRequest,
    MfaPasswordCodeRequest,
    MfaRecoveryCodesResponse,
    MfaStatusResponse,
    UserResponse,
)
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserResponse | MfaChallengeResponse, dependencies=[Depends(verify_csrf)])
async def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession, settings: AppSettings
) -> UserResponse | MfaChallengeResponse:
    result = await auth.login(
        db, settings, payload.email, payload.password.get_secret_value(), audit_context(request, None)
    )
    if result.mfa_challenge is not None:
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return MfaChallengeResponse(mfa_required=True, challenge_token=result.mfa_challenge)
    assert result.session_token is not None
    # HttpOnly: unreadable by JavaScript. SameSite=Strict: never sent on cross-site requests.
    response.set_cookie(
        SESSION_COOKIE_NAME,
        result.session_token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    return UserResponse(
        id=result.user.id,
        email=result.user.email,
        role=result.user.role,
        mfa_enrollment_required=result.mfa_enrollment_required,
    )


@router.post("/mfa/challenge", response_model=UserResponse, dependencies=[Depends(verify_csrf)])
async def verify_mfa_challenge(
    payload: MfaChallengeRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> UserResponse:
    user, token = await auth.complete_mfa_login(
        db,
        settings,
        payload.challenge_token.get_secret_value(),
        payload.code.get_secret_value(),
        request.app.state.cipher,
        audit_context(request, None),
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(id=user.id, email=user.email, role=user.role)


@router.get("/mfa/status", response_model=MfaStatusResponse)
async def mfa_status(
    response: Response, db: DbSession, user: CurrentUser, settings: AppSettings
) -> MfaStatusResponse:
    enabled, remaining = await auth.mfa_status(db, user)
    response.headers["Cache-Control"] = "no-store"
    return MfaStatusResponse(
        enabled=enabled,
        recovery_codes_remaining=remaining,
        required_by_organization=settings.org_mfa_required,
    )


@router.post("/mfa/enrollment", response_model=MfaEnrollmentResponse)
async def begin_mfa_enrollment(
    payload: MfaEnrollmentStartRequest, request: Request, response: Response, db: DbSession, user: CurrentUser
) -> MfaEnrollmentResponse:
    secret, uri = await auth.begin_mfa_enrollment(
        db,
        user,
        payload.password.get_secret_value(),
        request.app.state.cipher,
        audit_context(request, user),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return MfaEnrollmentResponse(secret=secret, provisioning_uri=uri)


@router.post("/mfa/enrollment/confirm", response_model=MfaRecoveryCodesResponse)
async def confirm_mfa_enrollment(
    payload: MfaCodeRequest, request: Request, response: Response, db: DbSession, user: CurrentUser
) -> MfaRecoveryCodesResponse:
    codes = await auth.confirm_mfa_enrollment(
        db,
        user,
        payload.code.get_secret_value(),
        request.app.state.cipher,
        request.cookies.get(SESSION_COOKIE_NAME, ""),
        audit_context(request, user),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return MfaRecoveryCodesResponse(recovery_codes=codes)


@router.post("/mfa/recovery-codes", response_model=MfaRecoveryCodesResponse)
async def rotate_mfa_recovery_codes(
    payload: MfaPasswordCodeRequest,
    request: Request,
    response: Response,
    db: DbSession,
    user: CurrentUser,
) -> MfaRecoveryCodesResponse:
    codes = await auth.rotate_mfa_recovery_codes(
        db,
        user,
        payload.password.get_secret_value(),
        payload.code.get_secret_value(),
        request.app.state.cipher,
        audit_context(request, user),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return MfaRecoveryCodesResponse(recovery_codes=codes)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_mfa(
    payload: MfaPasswordCodeRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> Response:
    if settings.org_mfa_required:
        raise PermissionDeniedError("MFA cannot be disabled while organization policy requires it.")
    await auth.disable_mfa(
        db,
        user,
        payload.password.get_secret_value(),
        payload.code.get_secret_value(),
        request.app.state.cipher,
        request.cookies.get(SESSION_COOKIE_NAME, ""),
        audit_context(request, user),
    )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings) -> Response:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    await auth.logout(db, token, audit_context(request, user))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE_NAME, path="/", httponly=True, secure=settings.session_cookie_secure, samesite="strict"
    )
    return response


@router.get("/me", response_model=UserResponse)
async def me(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings) -> UserResponse:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    enrollment_required, reauthentication_required = await auth.mfa_policy_state(db, user, token, settings)
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        mfa_enrollment_required=enrollment_required,
        mfa_reauthentication_required=reauthentication_required,
        capabilities=await _capabilities(db, user),
    )


async def _capabilities(db, user) -> list[str]:
    """Which delegable business capabilities the UI may offer; every route still checks on its own."""
    from app.core.permission_catalog import DEPARTMENT_CAPABILITIES, PERMISSION_REPORTS_VIEW
    from app.db.models import ROLE_ADMIN
    from app.services.access_control import delegated_department_scope, find_active_grant

    if user.role == ROLE_ADMIN:
        return [PERMISSION_REPORTS_VIEW, *DEPARTMENT_CAPABILITIES]
    held = []
    if await find_active_grant(db, user.id, PERMISSION_REPORTS_VIEW, "general", None) is not None:
        held.append(PERMISSION_REPORTS_VIEW)
    for permission in DEPARTMENT_CAPABILITIES:
        if await delegated_department_scope(db, user.id, permission) != set():
            held.append(permission)
    return held
