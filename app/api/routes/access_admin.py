"""Admin-only access control: groups, membership, permission grants and the ACL quarantine queue.
Administering access never grants the administrator read access to any document."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import AdminUser, DbSession, audit_context, require_admin
from app.db.models import PermissionGrant
from app.schemas.access import (
    AddMemberRequest,
    CreateGrantRequest,
    CreateGroupRequest,
    GrantListResponse,
    GrantOut,
    PermissionCatalogEntry,
    PermissionCatalogResponse,
    GroupListResponse,
    GroupOut,
    UpdateGroupRequest,
    WorkspaceSummaryReport,
    MemberListResponse,
    MemberOut,
    QuarantinedDocumentOut,
    QuarantineResponse,
)
from app.core.permission_catalog import PERMISSION_CATALOG
from app.services.access_control import AccessAdminService

router = APIRouter(prefix="/api/admin/access", tags=["access"], dependencies=[Depends(require_admin)])


def get_access_admin(request: Request, db: DbSession, admin: AdminUser) -> AccessAdminService:
    return AccessAdminService(db, admin, audit_context(request, admin))


Access = Annotated[AccessAdminService, Depends(get_access_admin)]


def _grant_out(grant: PermissionGrant, email: str) -> GrantOut:
    return GrantOut(
        id=grant.id, user_id=grant.user_id, email=email, permission=grant.permission, category=grant.category,
        scope_group_id=grant.scope_group_id, granted_by=grant.granted_by, granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
    )


def _group_out(g, member_count: int = 0) -> GroupOut:
    return GroupOut(
        id=g.id,
        name=g.name,
        kind=g.kind,
        source=g.source,
        external_id=g.external_id,
        member_count=member_count,
        created_at=g.created_at,
        parent_id=getattr(g, "parent_id", None),
        owner_id=getattr(g, "owner_id", None),
        lifecycle_status=getattr(g, "lifecycle_status", "active"),
    )


@router.get("/groups", response_model=GroupListResponse)
async def list_groups(service: Access) -> GroupListResponse:
    return GroupListResponse(groups=[_group_out(g, n) for g, n in await service.list_groups()])


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(payload: CreateGroupRequest, service: Access) -> GroupOut:
    g = await service.create_group(
        payload.name,
        payload.kind,
        payload.external_id,
        parent_id=payload.parent_id,
        owner_id=payload.owner_id,
    )
    return _group_out(g, member_count=0)


@router.patch("/groups/{group_id}", response_model=GroupOut)
async def update_group(group_id: uuid.UUID, payload: UpdateGroupRequest, service: Access) -> GroupOut:
    g = await service.update_group(
        group_id,
        name=payload.name,
        parent_id=payload.parent_id,
        owner_id=payload.owner_id,
        lifecycle_status=payload.lifecycle_status,
        parent_id_is_set="parent_id" in payload.model_fields_set,
        owner_id_is_set="owner_id" in payload.model_fields_set,
    )
    members = await service.members(group_id)
    return _group_out(g, member_count=len(members))


@router.get("/groups/{group_id}/summary", response_model=WorkspaceSummaryReport)
async def workspace_summary(group_id: uuid.UUID, service: Access) -> WorkspaceSummaryReport:
    data = await service.workspace_summary(group_id)
    return WorkspaceSummaryReport(**data)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, service: Access) -> Response:
    await service.delete_group(group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/groups/{group_id}/members", response_model=MemberListResponse)
async def list_members(group_id: uuid.UUID, service: Access) -> MemberListResponse:
    return MemberListResponse(members=[MemberOut(user_id=u.id, email=u.email) for u in await service.members(group_id)])


@router.post("/groups/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(group_id: uuid.UUID, payload: AddMemberRequest, service: Access) -> Response:
    await service.add_member(group_id, payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(group_id: uuid.UUID, user_id: uuid.UUID, service: Access) -> Response:
    await service.remove_member(group_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/grants", response_model=GrantListResponse)
async def list_grants(service: Access, include_revoked: Annotated[bool, Query()] = False) -> GrantListResponse:
    return GrantListResponse(grants=[_grant_out(g, e) for g, e in await service.list_grants(include_revoked)])


@router.get("/permission-catalog", response_model=PermissionCatalogResponse)
async def permission_catalog() -> PermissionCatalogResponse:
    return PermissionCatalogResponse(
        permissions=[
            PermissionCatalogEntry(
                permission=item.permission,
                resource=item.resource,
                action=item.action,
                scope_dimensions=list(item.scope_dimensions),
            )
            for item in PERMISSION_CATALOG
        ]
    )


@router.post("/grants", response_model=GrantOut, status_code=status.HTTP_201_CREATED)
async def create_grant(payload: CreateGrantRequest, service: Access) -> GrantOut:
    return _grant_out(*await service.grant(payload.email, payload.permission, payload.category, payload.scope_group_id))


@router.post("/grants/{grant_id}/revoke", response_model=GrantOut)
async def revoke_grant(grant_id: uuid.UUID, service: Access) -> GrantOut:
    return _grant_out(*await service.revoke(grant_id))


@router.get("/quarantine", response_model=QuarantineResponse)
async def quarantine(service: Access) -> QuarantineResponse:
    return QuarantineResponse(
        documents=[
            QuarantinedDocumentOut(
                id=d.id, source_system=d.source_system, reason=d.acl_quarantine_reason or "unknown",
                acl_synced_at=d.acl_synced_at, created_at=d.created_at,
            )
            for d in await service.quarantined_documents()
        ]
    )
