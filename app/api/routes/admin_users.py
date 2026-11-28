"""Administrator-managed accounts and secure employee onboarding."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import AppSettings, DbSession, PlatformAdminUser, audit_context, require_platform_admin
from app.schemas.users import (
    ActiveStateRequest,
    CsvInvitationPreviewRequest,
    CsvInvitationPreviewResponse,
    DepartmentAssignment,
    InvitationListResponse,
    InviteUserRequest,
    KnowledgeTransferRequest,
    KnowledgeTransferResponse,
    UpdateUserRequest,
    UserInvitationOut,
    UserListResponse,
    UserOut,
)
from app.services.users import UserAdminService
from app.services.deployment import license_claims

router = APIRouter(prefix="/api/admin", tags=["admin-users"], dependencies=[Depends(require_platform_admin)])


def _service(db: DbSession, admin: PlatformAdminUser, request: Request) -> UserAdminService:
    return UserAdminService(db, admin, audit_context(request, admin))


def _user_out(user, departments) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        departments=[DepartmentAssignment(id=group.id, name=group.name, manager=manager) for group, manager in departments],
    )


def _invitation_out(invitation, token: str | None = None) -> UserInvitationOut:
    return UserInvitationOut(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        department_ids=invitation.department_ids,
        role_template_id=invitation.role_template_id,
        position_id=invitation.position_id,
        reports_to_id=invitation.reports_to_id,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        token=token,
    )


@router.get("/users", response_model=UserListResponse)
async def list_users(service: Annotated[UserAdminService, Depends(_service)]) -> UserListResponse:
    return UserListResponse(users=[_user_out(user, depts) for user, depts in await service.list_users()])


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UpdateUserRequest,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
) -> UserOut:
    service = UserAdminService(db, admin, audit_context(request, admin))
    user = await service.update_role_and_departments(user_id, payload.role, payload.department_ids)
    for candidate, departments in await service.list_users():
        if candidate.id == user.id:
            return _user_out(user, departments)
    return _user_out(user, [])


@router.post("/users/{user_id}/active", response_model=UserOut)
async def set_user_active(
    user_id: uuid.UUID,
    payload: ActiveStateRequest,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
) -> UserOut:
    service = UserAdminService(db, admin, audit_context(request, admin))
    user = await service.set_active(user_id, payload.active, payload.knowledge_transfer_to)
    for candidate, departments in await service.list_users():
        if candidate.id == user.id:
            return _user_out(user, departments)
    return _user_out(user, [])


@router.post("/users/{user_id}/knowledge-transfer", response_model=KnowledgeTransferResponse)
async def transfer_user_knowledge(
    user_id: uuid.UUID,
    payload: KnowledgeTransferRequest,
    db: DbSession,
    admin: PlatformAdminUser,
    request: Request,
) -> KnowledgeTransferResponse:
    count = await UserAdminService(db, admin, audit_context(request, admin)).transfer_knowledge(
        user_id, payload.successor_user_id
    )
    return KnowledgeTransferResponse(transferred_entries=count)


@router.get("/users/invitations", response_model=InvitationListResponse)
async def list_invitations(service: Annotated[UserAdminService, Depends(_service)]) -> InvitationListResponse:
    return InvitationListResponse(invitations=[_invitation_out(row) for row in await service.list_invitations()])


@router.post("/users/invitations", response_model=UserInvitationOut, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: InviteUserRequest,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
    settings: AppSettings,
) -> UserInvitationOut:
    claims = license_claims(settings)
    invitation, token = await UserAdminService(db, admin, audit_context(request, admin)).create_invitation(
        payload.email,
        payload.role,
        payload.department_ids,
        seat_limit=claims.seat_limit if claims else None,
        role_template_id=payload.role_template_id,
        position_id=payload.position_id,
        reports_to_id=payload.reports_to_id,
    )
    return _invitation_out(invitation, token)


@router.post("/users/invitations/csv-preview", response_model=CsvInvitationPreviewResponse)
async def preview_invitation_csv(
    payload: CsvInvitationPreviewRequest,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
    settings: AppSettings,
) -> CsvInvitationPreviewResponse:
    claims = license_claims(settings)
    return await UserAdminService(db, admin, audit_context(request, admin)).preview_invitation_csv(
        payload.csv_text, seat_limit=claims.seat_limit if claims else None
    )


@router.post("/users/invitations/{invitation_id}/resend", response_model=UserInvitationOut)
async def resend_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
) -> UserInvitationOut:
    invitation, token = await UserAdminService(db, admin, audit_context(request, admin)).resend_invitation(invitation_id)
    return _invitation_out(invitation, token)


@router.post("/users/invitations/{invitation_id}/revoke", response_model=UserInvitationOut)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: PlatformAdminUser,
) -> UserInvitationOut:
    invitation = await UserAdminService(db, admin, audit_context(request, admin)).revoke_invitation(invitation_id)
    return _invitation_out(invitation)
