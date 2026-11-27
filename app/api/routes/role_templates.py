"""Admin API for scoped permission templates; Admin/Manager/Employee remain built-in roles."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import AdminUser, DbSession, audit_context, require_admin
from app.schemas.roles import (
    AssignRoleTemplateRequest,
    CreateRoleTemplateRequest,
    RoleTemplateActiveRequest,
    RoleTemplateAssignmentListResponse,
    RoleTemplateAssignmentOut,
    RoleTemplateChangeCreateRequest,
    RoleTemplateChangeDecisionRequest,
    RoleTemplateChangeListResponse,
    RoleTemplateChangeOut,
    RoleTemplateListResponse,
    RoleTemplateOut,
)
from app.services.role_templates import RoleTemplateService


router = APIRouter(prefix="/api/admin", tags=["admin-role-templates"], dependencies=[Depends(require_admin)])


def _service(db: DbSession, admin: AdminUser, request: Request) -> RoleTemplateService:
    return RoleTemplateService(db, admin, audit_context(request, admin))


def _template_out(row) -> RoleTemplateOut:
    return RoleTemplateOut(
        id=row.id,
        name=row.name,
        description=row.description,
        base_role=row.base_role,
        version=row.version,
        permissions=row.permission_specs,
        is_active=row.is_active,
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
    )


def _change_out(request_row, template, requester_email: str, decider_email: str | None) -> RoleTemplateChangeOut:
    return RoleTemplateChangeOut(
        id=request_row.id,
        role_template_id=template.id,
        template_name=template.name,
        current_version=template.version,
        requested_by=request_row.requested_by,
        requested_by_email=requester_email,
        proposed_description=request_row.proposed_description,
        proposed_permissions=request_row.proposed_permission_specs,
        status=request_row.status,
        decided_by=request_row.decided_by,
        decided_by_email=decider_email,
        decision_rationale=request_row.decision_rationale,
        new_template_id=request_row.new_template_id,
        created_at=request_row.created_at,
        decided_at=request_row.decided_at,
    )


def _assignment_out(row) -> RoleTemplateAssignmentOut:
    assignment, template, user, grant_count = row
    return RoleTemplateAssignmentOut(
        id=assignment.id,
        role_template_id=template.id,
        template_name=template.name,
        template_version=template.version,
        user_id=user.id,
        email=user.email,
        base_role=user.role,
        department_ids=assignment.department_ids,
        grant_count=grant_count,
        created_at=assignment.created_at,
        expires_at=assignment.expires_at,
        revoked_at=assignment.revoked_at,
    )


@router.get("/role-templates", response_model=RoleTemplateListResponse)
async def list_role_templates(service: Annotated[RoleTemplateService, Depends(_service)]) -> RoleTemplateListResponse:
    return RoleTemplateListResponse(templates=[_template_out(row) for row in await service.list_templates()])


@router.get("/role-library")
async def role_library() -> dict:
    """Ready-made roles an administrator can add with one click (also used by industry templates)."""
    from app.services.role_library import ROLE_LIBRARY

    return {"roles": [{"key": key, **role} for key, role in ROLE_LIBRARY.items()]}


@router.post("/role-templates", response_model=RoleTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_role_template(
    payload: CreateRoleTemplateRequest,
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateOut:
    row = await service.create_template(
        payload.name,
        payload.description,
        payload.base_role,
        [spec.model_dump() for spec in payload.permissions],
    )
    return _template_out(row)


@router.patch("/role-templates/{template_id}/active", response_model=RoleTemplateOut)
async def set_role_template_active(
    template_id: uuid.UUID,
    payload: RoleTemplateActiveRequest,
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateOut:
    return _template_out(await service.set_template_active(template_id, payload.active))


@router.get("/role-template-change-requests", response_model=RoleTemplateChangeListResponse)
async def list_role_template_change_requests(
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateChangeListResponse:
    rows = await service.list_changes()
    return RoleTemplateChangeListResponse(
        requests=[_change_out(request_row, template, requester_email, decider_email)
                  for request_row, template, requester_email, decider_email in rows]
    )


@router.post(
    "/role-templates/{template_id}/change-requests",
    response_model=RoleTemplateChangeOut,
    status_code=status.HTTP_201_CREATED,
)
async def propose_role_template_change(
    template_id: uuid.UUID,
    payload: RoleTemplateChangeCreateRequest,
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateChangeOut:
    request_row = await service.propose_change(
        template_id,
        payload.description,
        [spec.model_dump() for spec in payload.permissions],
    )
    rows = await service.list_changes()
    matched = next((row for row in rows if row[0].id == request_row.id), None)
    if matched is None:
        raise RuntimeError("Role-template change request could not be reloaded.")
    return _change_out(*matched)


@router.post(
    "/role-template-change-requests/{request_id}/decision",
    response_model=RoleTemplateChangeOut,
)
async def decide_role_template_change(
    request_id: uuid.UUID,
    payload: RoleTemplateChangeDecisionRequest,
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateChangeOut:
    request_row, template, _, _ = await service.decide_change(request_id, payload.decision, payload.rationale)
    rows = await service.list_changes()
    matched = next((row for row in rows if row[0].id == request_row.id), None)
    if matched is None:
        raise RuntimeError("Role-template change request could not be reloaded.")
    return _change_out(*matched)


@router.get("/role-template-assignments", response_model=RoleTemplateAssignmentListResponse)
async def list_role_template_assignments(
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateAssignmentListResponse:
    rows = await service.list_assignments()
    return RoleTemplateAssignmentListResponse(assignments=[_assignment_out(row) for row in rows])


@router.post(
    "/role-templates/{template_id}/assignments",
    response_model=RoleTemplateAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_role_template(
    template_id: uuid.UUID,
    payload: AssignRoleTemplateRequest,
    service: Annotated[RoleTemplateService, Depends(_service)],
) -> RoleTemplateAssignmentOut:
    row = await service.assign(template_id, payload.email, payload.department_ids, payload.expires_at)
    return _assignment_out(row)


@router.post("/role-template-assignments/{assignment_id}/revoke", response_model=RoleTemplateAssignmentOut)
async def revoke_role_template_assignment(
    assignment_id: uuid.UUID,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> RoleTemplateAssignmentOut:
    service = RoleTemplateService(db, admin, audit_context(request, admin))
    assignment, grant_count = await service.revoke_assignment(assignment_id)
    rows = await service.list_assignments(include_revoked=True)
    row = next((item for item in rows if item[0].id == assignment.id), None)
    if row is None:
        raise RuntimeError("Revoked role assignment could not be reloaded.")
    return _assignment_out(row)
