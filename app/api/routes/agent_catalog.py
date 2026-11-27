"""Authenticated read-only catalog plus explicit publisher, reviewer and revoker grants."""

import uuid
from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import NotFoundError
from app.db.models import ROLE_ADMIN, ROLE_MANAGER
from app.schemas.agent_catalog import (
    CatalogDraftCreatedOut,
    CatalogGrantListResponse,
    CatalogGrantOut,
    CatalogGrantRequest,
    CatalogManageListResponse,
    CatalogPublicListResponse,
    CatalogReviewRequest,
    CatalogRevocationRequest,
    CatalogRevokedOut,
    CatalogTemplateCreateRequest,
    CatalogTemplateOut,
    CatalogVersionRequest,
    CatalogVersionLifecycleOut,
)
from app.services import agent_catalog

router = APIRouter(
    prefix="/api/agent-catalog",
    tags=["agent-catalog"],
    dependencies=[Depends(get_current_user)],
)

@router.get("", response_model=CatalogPublicListResponse)
async def list_published_catalog(db: DbSession, user: CurrentUser) -> CatalogPublicListResponse:
    rows = await agent_catalog.list_public(db)
    can_publish, can_review = await agent_catalog.permission_flags(db, user)
    return CatalogPublicListResponse(
        items=[
            CatalogTemplateOut(
                id=template.id,
                slug=template.slug,
                version=version.version,
                name=version.name,
                summary=version.summary,
                agent_type=version.agent_type,
                knowledge_scope=version.knowledge_scope,
                tool_policy=version.tool_policy,
                published_at=version.reviewed_at,
                can_instantiate=user.role in {ROLE_ADMIN, ROLE_MANAGER},
            )
            for template, version in rows
        ],
        can_publish=can_publish,
        can_review=can_review,
    )


@router.get("/manage", response_model=CatalogManageListResponse)
async def list_managed_catalog(db: DbSession, user: CurrentUser) -> CatalogManageListResponse:
    items, can_publish, can_review = await agent_catalog.list_managed(db, user)
    return CatalogManageListResponse(items=items, can_publish=can_publish, can_review=can_review)


@router.post("/templates", response_model=CatalogDraftCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_catalog_template(
    payload: CatalogTemplateCreateRequest, db: DbSession, user: CurrentUser, request: Request
) -> CatalogDraftCreatedOut:
    template, version = await agent_catalog.create_template(
        db, user, audit_context(request, user), slug=payload.slug, name=payload.name,
        summary=payload.summary, agent_type=payload.agent_type, knowledge_scope=payload.knowledge_scope,
        tool_policy=payload.tool_policy,
    )
    return CatalogDraftCreatedOut(
        template_id=template.id, version_id=version.id, slug=template.slug,
        version=version.version, status="draft",
    )


@router.post(
    "/templates/{template_id}/versions",
    response_model=CatalogDraftCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_catalog_version(
    template_id: uuid.UUID, payload: CatalogVersionRequest, db: DbSession, user: CurrentUser, request: Request
) -> CatalogDraftCreatedOut:
    template, version = await agent_catalog.create_version(
        db, user, audit_context(request, user), template_id, name=payload.name, summary=payload.summary,
        agent_type=payload.agent_type, knowledge_scope=payload.knowledge_scope, tool_policy=payload.tool_policy,
    )
    return CatalogDraftCreatedOut(
        template_id=template.id, version_id=version.id, slug=template.slug,
        version=version.version, status="draft",
    )


@router.post("/versions/{version_id}/submit", response_model=CatalogVersionLifecycleOut)
async def submit_catalog_version(
    version_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> CatalogVersionLifecycleOut:
    version = await agent_catalog.submit_version(db, user, audit_context(request, user), version_id)
    return CatalogVersionLifecycleOut(
        id=version.id, template_id=version.template_id, version=version.version, status=version.status,
        submitted_at=version.submitted_at, reviewed_by_user_id=version.reviewed_by_user_id,
        reviewed_at=version.reviewed_at, review_rationale=version.review_rationale,
    )


@router.post("/versions/{version_id}/review", response_model=CatalogVersionLifecycleOut)
async def review_catalog_version(
    version_id: uuid.UUID, payload: CatalogReviewRequest, db: DbSession, user: CurrentUser, request: Request
) -> CatalogVersionLifecycleOut:
    version = await agent_catalog.review_version(
        db, user, audit_context(request, user), version_id,
        decision=payload.decision, rationale=payload.rationale,
    )
    return CatalogVersionLifecycleOut(
        id=version.id, template_id=version.template_id, version=version.version, status=version.status,
        submitted_at=version.submitted_at, reviewed_by_user_id=version.reviewed_by_user_id,
        reviewed_at=version.reviewed_at, review_rationale=version.review_rationale,
    )


@router.post("/templates/{template_id}/revoke", response_model=CatalogRevokedOut)
async def revoke_catalog_template(
    template_id: uuid.UUID,
    payload: CatalogRevocationRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> CatalogRevokedOut:
    template = await agent_catalog.revoke_template(
        db, user, audit_context(request, user), template_id, payload.rationale
    )
    return CatalogRevokedOut(
        id=template.id, slug=template.slug, revoked_at=template.revoked_at,
        revoked_by_user_id=template.revoked_by_user_id, revoke_rationale=template.revoke_rationale,
    )


@router.get("/grants", response_model=CatalogGrantListResponse)
async def list_catalog_grants(db: DbSession, user: CurrentUser) -> CatalogGrantListResponse:
    return CatalogGrantListResponse(items=await agent_catalog.list_grants(db, user))


@router.post("/grants", response_model=CatalogGrantOut, status_code=status.HTTP_201_CREATED)
async def create_catalog_grant(
    payload: CatalogGrantRequest, db: DbSession, user: CurrentUser, request: Request
) -> CatalogGrantOut:
    grant = await agent_catalog.create_grant(
        db, user, audit_context(request, user), email=payload.email, permission=payload.permission
    )
    result = next(item for item in await agent_catalog.list_grants(db, user) if item["id"] == grant.id)
    return CatalogGrantOut(**result)


@router.post("/grants/{grant_id}/revoke", response_model=CatalogGrantOut)
async def revoke_catalog_grant(
    grant_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> CatalogGrantOut:
    grant = await agent_catalog.revoke_grant(db, user, audit_context(request, user), grant_id)
    result = next(item for item in await agent_catalog.list_grants(db, user) if item["id"] == grant.id)
    return CatalogGrantOut(**result)
