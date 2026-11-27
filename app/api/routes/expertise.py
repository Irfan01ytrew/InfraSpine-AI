"""Self-managed skills and permission-aware expertise search."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentUser, DbSession, audit_context
from app.schemas.expertise import ExpertiseProfile, ExpertiseSearchResponse, ExpertiseUpdate
from app.services.expertise import ExpertiseService

router = APIRouter(prefix="/api/expertise", tags=["expertise"])


@router.get("/me", response_model=ExpertiseProfile)
async def my_expertise(user: CurrentUser) -> ExpertiseProfile:
    return ExpertiseProfile(user_id=str(user.id), tags=list(user.expertise_tags or []))


@router.put("/me", response_model=ExpertiseProfile)
async def update_my_expertise(
    payload: ExpertiseUpdate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ExpertiseProfile:
    service = ExpertiseService(db, user, audit_context(request, user))
    return await service.update_profile(payload.tags)


@router.get("/search", response_model=ExpertiseSearchResponse)
async def search_expertise(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    skill: Annotated[str, Query(min_length=2, max_length=60)],
    after: Annotated[str | None, Query(min_length=1, max_length=320)] = None,
) -> ExpertiseSearchResponse:
    return await ExpertiseService(db, user, audit_context(request, user)).search(skill, after=after)
