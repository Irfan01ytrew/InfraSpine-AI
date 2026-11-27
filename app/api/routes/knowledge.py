"""Draft organizational knowledge. Routes only return the signed-in steward's entries (others get 404).
Nothing here publishes a revision or writes the knowledge index."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import KnowledgeEvidence, KnowledgeRevision
from app.schemas.knowledge import (
    CreateKnowledgeRequest,
    EvidenceOut,
    KnowledgeDetail,
    KnowledgeListResponse,
    KnowledgeOut,
    RevisionOut,
)
from app.services.knowledge import KnowledgeService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=[Depends(get_current_user)])


def get_knowledge_service(request: Request, db: DbSession, user: CurrentUser) -> KnowledgeService:
    return KnowledgeService(db, user, audit_context(request, user), request.app.state.rag.embedder)


Knowledge = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def _evidence(row: KnowledgeEvidence) -> EvidenceOut:
    return EvidenceOut(
        id=row.id,
        document_id=row.document_id,
        document_key=row.document_key,
        document_version=row.document_version,
        filename=row.filename,
        chunk_key=row.chunk_key,
        page_number=row.page_number,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        quote=row.quote,
        prefix=row.prefix,
        suffix=row.suffix,
        orphan_reason=row.orphan_reason,  # type: ignore[arg-type]
        orphaned_at=row.orphaned_at,
    )


def _revision(row: KnowledgeRevision, evidence: list[KnowledgeEvidence]) -> RevisionOut:
    return RevisionOut(
        id=row.id,
        revision_number=row.revision_number,
        statement=row.statement,
        category=row.category,  # type: ignore[arg-type]
        visibility=row.visibility,  # type: ignore[arg-type]
        scope_group_id=row.scope_group_id,
        status=row.status,  # type: ignore[arg-type]
        created_at=row.created_at,
        published=row.status == "published",
        evidence=[_evidence(item) for item in evidence if item.revision_id == row.id],
    )


@router.get("", response_model=KnowledgeListResponse)
async def list_knowledge(service: Knowledge) -> KnowledgeListResponse:
    items = []
    for entry, current in await service.list_own():
        _, _, evidence = await service.get_own(entry.id)
        items.append(
            KnowledgeOut(id=entry.id, created_at=entry.created_at, current=_revision(current, evidence))
        )
    return KnowledgeListResponse(items=items)


@router.post("", response_model=KnowledgeDetail, status_code=status.HTTP_201_CREATED)
async def create_knowledge(payload: CreateKnowledgeRequest, service: Knowledge) -> KnowledgeDetail:
    entry = await service.create(
        statement=payload.statement,
        category=payload.category,
        visibility=payload.visibility,
        scope_group_id=payload.scope_group_id,
        passages=[(item.document_id, item.quote) for item in payload.evidence],
    )
    return await _detail(service, entry.id)


@router.get("/{entry_id}", response_model=KnowledgeDetail)
async def get_knowledge(entry_id: uuid.UUID, service: Knowledge) -> KnowledgeDetail:
    return await _detail(service, entry_id)


@router.post("/{entry_id}/revisions", response_model=KnowledgeDetail)
async def add_revision(entry_id: uuid.UUID, payload: CreateKnowledgeRequest, service: Knowledge) -> KnowledgeDetail:
    await service.add_revision(
        entry_id,
        statement=payload.statement,
        category=payload.category,
        visibility=payload.visibility,
        scope_group_id=payload.scope_group_id,
        passages=[(item.document_id, item.quote) for item in payload.evidence],
    )
    return await _detail(service, entry_id)


@router.post("/{entry_id}/withdraw", response_model=KnowledgeDetail)
async def withdraw_knowledge(entry_id: uuid.UUID, service: Knowledge) -> KnowledgeDetail:
    await service.withdraw(entry_id)
    return await _detail(service, entry_id)


@router.post("/{entry_id}/submit", response_model=KnowledgeDetail)
async def submit_knowledge(entry_id: uuid.UUID, service: Knowledge) -> KnowledgeDetail:
    await service.submit(entry_id)
    return await _detail(service, entry_id)


async def _detail(service: KnowledgeService, entry_id: uuid.UUID) -> KnowledgeDetail:
    entry, revisions, evidence = await service.get_own(entry_id)
    return KnowledgeDetail(
        id=entry.id,
        created_at=entry.created_at,
        revisions=[_revision(row, evidence) for row in revisions],
    )
