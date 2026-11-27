"""Employee knowledge handoffs. Content is visible only to its author, department managers and admins."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context
from app.db.models import AccessGroup
from app.schemas.handoffs import HandoffCreate, HandoffListResponse, HandoffOut
from app.services.handoffs import HandoffService

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])


def get_service(request: Request, db: DbSession, user: CurrentUser) -> HandoffService:
    return HandoffService(db, user, audit_context(request, user))


Handoffs = Annotated[HandoffService, Depends(get_service)]


def _out(handoff, department_name: str, acknowledged_by: str | None) -> HandoffOut:
    return HandoffOut(
        id=handoff.id,
        author_email=handoff.author_email,
        department_id=handoff.department_group_id,
        department_name=department_name,
        title=handoff.title,
        body=handoff.body,
        status=handoff.status,
        created_at=handoff.created_at,
        acknowledged_by=acknowledged_by,
        acknowledged_at=handoff.acknowledged_at,
    )


@router.get("", response_model=HandoffListResponse)
async def list_handoffs(service: Handoffs) -> HandoffListResponse:
    return HandoffListResponse(items=[_out(*row) for row in await service.list_readable()])


@router.post("", response_model=HandoffOut, status_code=status.HTTP_201_CREATED)
async def create_handoff(payload: HandoffCreate, service: Handoffs, db: DbSession) -> HandoffOut:
    handoff = await service.create(payload.department_id, payload.title, payload.body)
    group = await db.get(AccessGroup, handoff.department_group_id)
    return _out(handoff, group.name if group else "Department", None)


@router.post("/{handoff_id}/acknowledge", response_model=HandoffOut)
async def acknowledge(handoff_id: uuid.UUID, service: Handoffs, db: DbSession, user: CurrentUser) -> HandoffOut:
    handoff = await service.acknowledge(handoff_id)
    group = await db.get(AccessGroup, handoff.department_group_id)
    return _out(handoff, group.name if group else "Department", user.email)
