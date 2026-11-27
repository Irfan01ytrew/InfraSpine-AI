"""Guided employee interviews and department-scoped SOP review."""

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.schemas.knowledge_interviews import (
    InterviewOptionsOut,
    InterviewProgressUpdate,
    InterviewReviewRequest,
    InterviewSessionCreate,
    InterviewSessionListOut,
    InterviewSessionOut,
    InterviewTemplateCreate,
    InterviewTemplateOut,
    InterviewTemplateUpdate,
)
from app.services.knowledge_interviews import KnowledgeInterviewService

router = APIRouter(
    prefix="/api/knowledge-interviews",
    tags=["knowledge-interviews"],
    dependencies=[Depends(get_current_user)],
)


def service(db: DbSession, user: CurrentUser, request: Request) -> KnowledgeInterviewService:
    return KnowledgeInterviewService(db, user, audit_context(request, user))


@router.get("/options", response_model=InterviewOptionsOut)
async def interview_options(request: Request, db: DbSession, user: CurrentUser) -> InterviewOptionsOut:
    rows = await service(db, user, request).options()
    return InterviewOptionsOut(
        templates=[InterviewTemplateOut.model_validate(row) for row in rows["templates"]],
        managed_templates=[InterviewTemplateOut.model_validate(row) for row in rows["managed_templates"]],
        departments=[{"id": group.id, "name": group.name} for group in rows["departments"]],
        can_manage_templates=rows["can_manage_templates"],
    )


@router.post("/templates", response_model=InterviewTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: InterviewTemplateCreate, request: Request, db: DbSession, user: CurrentUser
) -> InterviewTemplateOut:
    row = await service(db, user, request).create_template(
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        questions=[question.model_dump() for question in payload.questions],
    )
    return InterviewTemplateOut.model_validate(row)


@router.patch("/templates/{template_id}", response_model=InterviewTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    payload: InterviewTemplateUpdate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> InterviewTemplateOut:
    values = payload.model_dump(exclude_unset=True)
    row = await service(db, user, request).update_template(template_id, values)
    return InterviewTemplateOut.model_validate(row)


@router.get("/sessions", response_model=InterviewSessionListOut)
async def list_sessions(request: Request, db: DbSession, user: CurrentUser) -> InterviewSessionListOut:
    manager = service(db, user, request)
    sessions = await manager.list_sessions()
    return InterviewSessionListOut(sessions=[await manager.view(row) for row in sessions])


@router.post("/sessions", response_model=InterviewSessionOut, status_code=status.HTTP_201_CREATED)
async def start_session(
    payload: InterviewSessionCreate, request: Request, db: DbSession, user: CurrentUser
) -> InterviewSessionOut:
    manager = service(db, user, request)
    row = await manager.start(payload.template_id, payload.department_group_id)
    return InterviewSessionOut(**await manager.view(row))


@router.get("/sessions/{session_id}", response_model=InterviewSessionOut)
async def get_session(session_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser) -> InterviewSessionOut:
    manager = service(db, user, request)
    return InterviewSessionOut(**await manager.view(await manager.get(session_id)))


@router.patch("/sessions/{session_id}", response_model=InterviewSessionOut)
async def save_session(
    session_id: uuid.UUID,
    payload: InterviewProgressUpdate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> InterviewSessionOut:
    manager = service(db, user, request)
    row = await manager.save_progress(session_id, payload.answers, payload.sop_title)
    return InterviewSessionOut(**await manager.view(row))


@router.post("/sessions/{session_id}/submit", response_model=InterviewSessionOut)
async def submit_session(
    session_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> InterviewSessionOut:
    manager = service(db, user, request)
    row = await manager.submit(session_id)
    return InterviewSessionOut(**await manager.view(row))


@router.post("/sessions/{session_id}/review", response_model=InterviewSessionOut)
async def review_session(
    session_id: uuid.UUID,
    payload: InterviewReviewRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> InterviewSessionOut:
    manager = service(db, user, request)
    row = await manager.review(session_id, payload.decision, payload.rationale)
    return InterviewSessionOut(**await manager.view(row))


@router.post("/sessions/{session_id}/withdraw", response_model=InterviewSessionOut)
async def withdraw_session(
    session_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> InterviewSessionOut:
    manager = service(db, user, request)
    row = await manager.withdraw(session_id)
    return InterviewSessionOut(**await manager.view(row))
