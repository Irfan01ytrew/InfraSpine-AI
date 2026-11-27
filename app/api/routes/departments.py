"""Department-scoped membership and security controls for managers."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import AppSettings, CurrentUser, DbSession, ManagerUser, audit_context
from app.schemas.access import AddMemberRequest, GroupListResponse, GroupOut, MemberListResponse, MemberOut
from app.schemas.users import InvitationListResponse, ManagerInviteEmployeeRequest, UserInvitationOut
from app.services.departments import DepartmentService
from app.services.deployment import license_claims

router = APIRouter(prefix="/api/departments", tags=["departments"])


def get_department_service(request: Request, db: DbSession, manager: CurrentUser) -> DepartmentService:
    return DepartmentService(db, manager, audit_context(request, manager))


Department = Annotated[DepartmentService, Depends(get_department_service)]


@router.get("/managed", response_model=GroupListResponse)
async def managed_departments(service: Department, manager: ManagerUser) -> GroupListResponse:
    groups = await service.managed()
    return GroupListResponse(
        groups=[
            GroupOut(
                id=group.id,
                name=group.name,
                kind=group.kind,
                source=group.source,
                external_id=group.external_id,
                member_count=len(await service.members(group.id)),
                created_at=group.created_at,
            )
            for group in groups
        ]
    )


@router.get("/mine", response_model=GroupListResponse)
async def my_departments(service: Department) -> GroupListResponse:
    groups = await service.mine()
    return GroupListResponse(
        groups=[
            GroupOut(
                id=group.id,
                name=group.name,
                kind=group.kind,
                source=group.source,
                external_id=group.external_id,
                member_count=0,
                created_at=group.created_at,
            )
            for group in groups
        ]
    )


@router.get("/{group_id}/members", response_model=MemberListResponse)
async def list_members(group_id: uuid.UUID, service: Department) -> MemberListResponse:
    return MemberListResponse(members=[MemberOut(user_id=user.id, email=user.email) for user in await service.members(group_id)])


@router.post("/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(group_id: uuid.UUID, payload: AddMemberRequest, service: Department) -> Response:
    await service.add_member(group_id, payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/invitable", response_model=GroupListResponse)
async def invitable_departments(service: Department) -> GroupListResponse:
    """Departments the caller can invite Employees into: managed ones, or those covered by people.invite."""
    return GroupListResponse(
        groups=[
            GroupOut(id=g.id, name=g.name, kind=g.kind, source=g.source, external_id=g.external_id, member_count=0, created_at=g.created_at)
            for g in await service.invitable()
        ]
    )


# Invitation routes authorize per department in the service: assigned manager, or a people.invite grant.
@router.post("/{group_id}/invitations", response_model=UserInvitationOut, status_code=status.HTTP_201_CREATED)
async def invite_employee(
    group_id: uuid.UUID,
    payload: ManagerInviteEmployeeRequest,
    service: Department,
    settings: AppSettings,
) -> UserInvitationOut:
    claims = license_claims(settings)
    invitation, token = await service.create_employee_invitation(
        group_id, payload.email, seat_limit=claims.seat_limit if claims else None
    )
    return UserInvitationOut(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        department_ids=invitation.department_ids,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        token=token,
    )


def _invitation_out(invitation, token: str | None = None) -> UserInvitationOut:
    return UserInvitationOut(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        department_ids=invitation.department_ids,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        token=token,
    )


@router.get("/{group_id}/invitations", response_model=InvitationListResponse)
async def department_invitations(group_id: uuid.UUID, service: Department) -> InvitationListResponse:
    rows = await service.invitations(group_id)
    return InvitationListResponse(invitations=[_invitation_out(row) for row in rows])


@router.post("/{group_id}/invitations/{invitation_id}/resend", response_model=UserInvitationOut)
async def resend_department_invitation(
    group_id: uuid.UUID, invitation_id: uuid.UUID, service: Department
) -> UserInvitationOut:
    invitation, token = await service.resend_invitation(group_id, invitation_id)
    return _invitation_out(invitation, token)


@router.post("/{group_id}/invitations/{invitation_id}/revoke", response_model=UserInvitationOut)
async def revoke_department_invitation(
    group_id: uuid.UUID, invitation_id: uuid.UUID, service: Department
) -> UserInvitationOut:
    invitation = await service.revoke_invitation(group_id, invitation_id)
    return _invitation_out(invitation)


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(group_id: uuid.UUID, user_id: uuid.UUID, service: Department) -> Response:
    await service.remove_member(group_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
