"""Unverified claims: every route acts only on the signed-in user's own submissions (others get 404)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import KnowledgeClaim, KnowledgeClaimEvent
from app.schemas.claims import (
    ClaimDetail,
    ClaimEventOut,
    ClaimListResponse,
    ClaimOut,
    CreateClaimRequest,
    DocumentSourceOut,
    NoteSourceOut,
    SourceOut,
    UrlSourceOut,
)
from app.services.claims import ClaimsService

router = APIRouter(prefix="/api/claims", tags=["claims"], dependencies=[Depends(get_current_user)])


def get_claims_service(request: Request, db: DbSession, user: CurrentUser) -> ClaimsService:
    return ClaimsService(db, user, audit_context(request, user))


Claims = Annotated[ClaimsService, Depends(get_claims_service)]


def _source_out(raw: dict) -> SourceOut:
    kind = raw["kind"]
    if kind == "document":
        return DocumentSourceOut(kind="document", document_id=uuid.UUID(raw["document_id"]), filename=raw["filename"])
    if kind == "url":
        return UrlSourceOut(kind="url", url=raw["url"])
    return NoteSourceOut(kind="note", note=raw["note"])


def _out(claim: KnowledgeClaim) -> ClaimOut:
    return ClaimOut(
        id=claim.id,
        kind=claim.kind,
        statement=claim.statement,
        category=claim.category,
        scope_group_id=claim.scope_group_id,
        sources=[_source_out(s) for s in claim.sources],
        status=claim.status,
        created_at=claim.created_at,
        withdrawn_at=claim.withdrawn_at,
        verified=False,
    )


def _event_out(event: KnowledgeClaimEvent) -> ClaimEventOut:
    return ClaimEventOut(
        id=event.id,
        action=event.action,
        from_status=event.from_status,  # type: ignore[arg-type]
        to_status=event.to_status,  # type: ignore[arg-type]
        created_at=event.created_at,
    )


@router.get("", response_model=ClaimListResponse)
async def list_claims(service: Claims) -> ClaimListResponse:
    return ClaimListResponse(items=[_out(c) for c in await service.list_own()])


@router.post("", response_model=ClaimOut, status_code=status.HTTP_201_CREATED)
async def create_claim(payload: CreateClaimRequest, service: Claims) -> ClaimOut:
    return _out(
        await service.submit(
            kind=payload.kind,
            statement=payload.statement,
            category=payload.category,
            scope_group_id=payload.scope_group_id,
            sources=[s.model_dump() for s in payload.sources],
        )
    )


@router.get("/{claim_id}", response_model=ClaimDetail)
async def get_claim(claim_id: uuid.UUID, service: Claims) -> ClaimDetail:
    claim = await service.get_own(claim_id)
    return ClaimDetail(**_out(claim).model_dump(), history=[_event_out(e) for e in await service.events(claim_id)])


@router.post("/{claim_id}/withdraw", response_model=ClaimOut)
async def withdraw_claim(claim_id: uuid.UUID, service: Claims) -> ClaimOut:
    return _out(await service.withdraw(claim_id))
