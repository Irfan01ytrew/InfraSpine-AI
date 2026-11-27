"""Personal memory: every route acts only on the signed-in user's own items (others get 404)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import InvalidInputError
from app.db.models import MemoryItem
from app.schemas.memory import (
    CreateMemoryRequest,
    MemoryClearResponse,
    MemoryListResponse,
    MemoryOut,
    MemorySaveResponse,
    UpdateMemoryRequest,
)
from app.services.memory import ACTIVE, MemoryService, SaveResult

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(get_current_user)])


def get_memory_service(request: Request, db: DbSession, settings: AppSettings, user: CurrentUser) -> MemoryService:
    return MemoryService(db, settings, user, audit_context(request, user))


Memories = Annotated[MemoryService, Depends(get_memory_service)]


def _out(item: MemoryItem) -> MemoryOut:
    return MemoryOut(
        id=item.id, kind=item.kind, subject=item.subject, content=item.content, status=item.status,
        source=item.source, supersedes_id=item.supersedes_id, created_at=item.created_at,
        superseded_at=item.superseded_at,
    )


def _saved(result: SaveResult) -> MemorySaveResponse:
    return MemorySaveResponse(
        item=_out(result.item), outcome=result.outcome, replaced_id=result.replaced.id if result.replaced else None
    )


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    service: Memories, settings: AppSettings, include_superseded: Annotated[bool, Query()] = False
) -> MemoryListResponse:
    items = await service.list_items(include_superseded)
    return MemoryListResponse(
        items=[_out(i) for i in items],
        active_count=sum(i.status == ACTIVE for i in items),
        max_items=settings.memory_max_items,
    )


@router.post("", response_model=MemorySaveResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(payload: CreateMemoryRequest, service: Memories) -> MemorySaveResponse:
    return _saved(await service.remember(payload.content, subject=payload.subject, kind=payload.kind))


@router.patch("/{item_id}", response_model=MemorySaveResponse)
async def update_memory(item_id: uuid.UUID, payload: UpdateMemoryRequest, service: Memories) -> MemorySaveResponse:
    if payload.content is None and payload.subject is None and payload.kind is None:
        raise InvalidInputError("Nothing to change.")
    return _saved(
        await service.update(
            item_id, content=payload.content, subject=payload.subject or None, kind=payload.kind,
            clear_subject=payload.subject == "",
        )
    )


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(item_id: uuid.UUID, service: Memories) -> Response:
    await service.delete(item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", response_model=MemoryClearResponse)
async def clear_memories(service: Memories, all: Annotated[bool, Query()] = False) -> MemoryClearResponse:  # noqa: A002
    if not all:
        raise InvalidInputError("Add ?all=true to delete every memory.")
    return MemoryClearResponse(deleted=await service.delete_all())
