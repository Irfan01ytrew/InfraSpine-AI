"""Public token-based onboarding. Invitations are single-use and expire after 72 hours."""

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbSession, audit_context, verify_csrf
from app.schemas.users import AcceptInvitationRequest, OnboardingAccepted
from app.services.users import accept_invitation

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("/accept", response_model=OnboardingAccepted, dependencies=[Depends(verify_csrf)])
async def accept(payload: AcceptInvitationRequest, request: Request, db: DbSession) -> OnboardingAccepted:
    user = await accept_invitation(
        db,
        payload.token,
        payload.password,
        audit_context(request, None),
    )
    return OnboardingAccepted(
        email=user.email,
        role=user.role,
        message="Your account is ready. Sign in with your email and new password.",
    )
