"""Authenticated API for editable, source-backed meeting records."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import Document, User
from app.schemas.meetings import (
    MeetingActionTaskOut,
    MeetingCreate,
    MeetingDetailOut,
    MeetingDraftSave,
    MeetingGenerateDraft,
    MeetingListOut,
    MeetingOut,
    MeetingReviewOut,
    MeetingReviewRequest,
    MeetingSourceList,
    MeetingSourceOut,
    MeetingSubmit,
    MeetingTranscriptOut,
)
from app.services.meetings import MeetingService, task_email

router = APIRouter(prefix="/api/meetings", tags=["meetings"], dependencies=[Depends(get_current_user)])


def get_service(request: Request, db: DbSession, user: CurrentUser) -> MeetingService:
    return MeetingService(db, user, audit_context(request, user))


Meetings = Annotated[MeetingService, Depends(get_service)]


def _meeting_out(meeting, source: Document, *, source_is_current: bool, can_edit: bool, can_review: bool) -> MeetingOut:
    return MeetingOut(
        id=meeting.id,
        source_document_id=source.id,
        source_filename=source.filename,
        source_document_version=meeting.source_document_version,
        source_is_current=source_is_current,
        owner_email=meeting.owner_email,
        title=meeting.title,
        status=meeting.status,
        current_revision=meeting.current_revision,
        submitted_revision_id=meeting.submitted_revision_id,
        created_at=meeting.created_at,
        updated_at=meeting.updated_at,
        can_edit=can_edit,
        can_review=can_review,
    )


async def _detail(service: MeetingService, meeting_id: uuid.UUID) -> MeetingDetailOut:
    data = await service.detail(meeting_id)
    meeting, source = data["meeting"], data["source"]
    return MeetingDetailOut(
        **_meeting_out(
            meeting,
            source,
            source_is_current=data["source_is_current"],
            can_edit=data["can_edit"],
            can_review=data["can_review"],
        ).model_dump(),
        revision=data["revision"],
        reviews=[
            MeetingReviewOut(
                decision=item.decision,
                rationale=item.rationale,
                reviewer_email=item.reviewer_email,
                reviewed_at=item.reviewed_at,
                revision_id=item.revision_id,
            )
            for item in data["reviews"]
        ],
        action_tasks=[task_email(task, email) for task, email in data["tasks"]],
        eligible_assignees=[{"id": user.id, "email": user.email} for user in data["eligible_assignees"]],
    )


@router.get("/sources", response_model=MeetingSourceList)
async def list_sources(service: Meetings) -> MeetingSourceList:
    return MeetingSourceList(
        sources=[
            MeetingSourceOut(
                id=item.id,
                filename=item.filename,
                file_format=item.file_format,
                version=item.version,
                chunk_count=item.chunk_count,
                indexed_at=item.indexed_at,
            )
            for item in await service.list_sources()
        ]
    )


@router.get("", response_model=MeetingListOut)
async def list_meetings(service: Meetings) -> MeetingListOut:
    values = []
    for meeting, source in await service.list():
        values.append(
            _meeting_out(
                meeting,
                source,
                source_is_current=service.source_is_current(meeting, source),
                can_edit=meeting.owner_user_id == service.user.id and meeting.status in {"draft", "changes_requested"},
                can_review=await service.can_review_meeting(meeting),
            )
        )
    return MeetingListOut(meetings=values)


@router.post("", response_model=MeetingDetailOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(payload: MeetingCreate, service: Meetings) -> MeetingDetailOut:
    meeting = await service.create(payload.source_document_id, payload.title)
    return await _detail(service, meeting.id)


@router.get("/{meeting_id}", response_model=MeetingDetailOut)
async def get_meeting(meeting_id: uuid.UUID, service: Meetings) -> MeetingDetailOut:
    return await _detail(service, meeting_id)


@router.get("/{meeting_id}/transcript", response_model=MeetingTranscriptOut)
async def get_transcript(
    meeting_id: uuid.UUID,
    service: Meetings,
    after_chunk_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MeetingTranscriptOut:
    source, chunks = await service.transcript(meeting_id, after_chunk_id, limit)
    has_more = len(chunks) > limit
    selected = chunks[:limit]
    return MeetingTranscriptOut(
        document_id=source.id,
        document_version=source.version,
        chunks=selected,
        next_after_chunk_id=selected[-1].id if has_more and selected else None,
    )


@router.put("/{meeting_id}/draft", response_model=MeetingDetailOut)
async def save_draft(meeting_id: uuid.UUID, payload: MeetingDraftSave, service: Meetings) -> MeetingDetailOut:
    await service.save_draft(
        meeting_id,
        expected_version=payload.expected_version,
        minutes_markdown=payload.minutes_markdown,
        decisions=[item.model_dump(mode="json") for item in payload.decisions],
        proposed_tasks=[item.model_dump(mode="json") for item in payload.proposed_tasks],
    )
    return await _detail(service, meeting_id)


@router.post("/{meeting_id}/generate-draft", response_model=MeetingDetailOut)
async def generate_draft(
    meeting_id: uuid.UUID,
    payload: MeetingGenerateDraft,
    request: Request,
    db: DbSession,
    service: Meetings,
) -> MeetingDetailOut:
    await service.generate_minutes(
        meeting_id,
        payload.expected_version,
        lambda: request.app.state.llm_runtime.get_client(db),
    )
    return await _detail(service, meeting_id)


@router.post("/{meeting_id}/submit", response_model=MeetingDetailOut)
async def submit_meeting(meeting_id: uuid.UUID, payload: MeetingSubmit, service: Meetings) -> MeetingDetailOut:
    await service.submit(meeting_id, payload.expected_version)
    return await _detail(service, meeting_id)


@router.post("/{meeting_id}/review", response_model=MeetingDetailOut)
async def review_meeting(
    meeting_id: uuid.UUID,
    payload: MeetingReviewRequest,
    service: Meetings,
) -> MeetingDetailOut:
    await service.review(meeting_id, payload.decision, payload.rationale)
    return await _detail(service, meeting_id)


@router.post("/tasks/{task_id}/complete", response_model=MeetingActionTaskOut)
async def complete_task(task_id: uuid.UUID, service: Meetings, db: DbSession, user: CurrentUser) -> MeetingActionTaskOut:
    task = await service.complete_task(task_id)
    assignee = await db.get(User, task.assignee_user_id)
    return MeetingActionTaskOut(**task_email(task, assignee.email if assignee else "Unavailable"))
