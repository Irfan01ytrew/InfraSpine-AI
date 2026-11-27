"""Strict request and response contracts for source-backed meeting records."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class MeetingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_document_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=180)


class MeetingDecisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(..., min_length=1, max_length=2000)
    evidence_chunk_ids: list[StrictInt] = Field(..., min_length=1, max_length=10)


class MeetingTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=180)
    body: str = Field(..., min_length=1, max_length=4000)
    assignee_user_id: uuid.UUID
    due_at: datetime | None = None
    evidence_chunk_ids: list[int] = Field(..., min_length=1, max_length=10)

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        return value


class MeetingDraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(..., ge=1)
    minutes_markdown: str = Field(..., max_length=40000)
    decisions: list[MeetingDecisionDraft] = Field(default_factory=list, max_length=30)
    proposed_tasks: list[MeetingTaskDraft] = Field(default_factory=list, max_length=30)


class MeetingSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)


class MeetingGenerateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)


class MeetingGeneratedPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(..., min_length=1, max_length=1200)
    evidence_chunk_ids: list[StrictInt] = Field(..., min_length=1, max_length=10)


class MeetingGeneratedMinutes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: list[MeetingGeneratedPoint] = Field(..., min_length=1, max_length=12)
    decisions: list[MeetingGeneratedPoint] = Field(default_factory=list, max_length=12)


class MeetingReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approved", "changes_requested"]
    rationale: str = Field(..., min_length=3, max_length=2000)


class MeetingSourceOut(BaseModel):
    id: uuid.UUID
    filename: str
    file_format: Literal["audio", "video"]
    version: int
    chunk_count: int
    indexed_at: datetime | None


class MeetingSourceList(BaseModel):
    sources: list[MeetingSourceOut]


class MeetingRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    editor_email: str
    minutes_markdown: str
    decisions: list[MeetingDecisionDraft]
    proposed_tasks: list[MeetingTaskDraft]
    created_at: datetime


class MeetingReviewOut(BaseModel):
    decision: Literal["approved", "changes_requested"]
    rationale: str
    reviewer_email: str
    reviewed_at: datetime
    revision_id: int


class MeetingActionTaskOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    assignee_user_id: uuid.UUID
    assignee_email: str
    evidence_chunk_ids: list[int]
    due_at: datetime | None
    status: Literal["open", "completed"]
    created_at: datetime
    completed_at: datetime | None


class MeetingAssigneeOut(BaseModel):
    id: uuid.UUID
    email: str


class MeetingOut(BaseModel):
    id: uuid.UUID
    source_document_id: uuid.UUID
    source_filename: str
    source_document_version: int
    source_is_current: bool
    owner_email: str
    title: str
    status: Literal["draft", "submitted", "changes_requested", "approved"]
    current_revision: int
    submitted_revision_id: int | None
    created_at: datetime
    updated_at: datetime
    can_edit: bool
    can_review: bool


class MeetingDetailOut(MeetingOut):
    revision: MeetingRevisionOut
    reviews: list[MeetingReviewOut]
    action_tasks: list[MeetingActionTaskOut]
    eligible_assignees: list[MeetingAssigneeOut]


class MeetingListOut(BaseModel):
    meetings: list[MeetingOut]


class MeetingTranscriptChunk(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    page_number: int
    locator: dict | None
    content: str


class MeetingTranscriptOut(BaseModel):
    document_id: uuid.UUID
    document_version: int
    chunks: list[MeetingTranscriptChunk]
    next_after_chunk_id: int | None


class MeetingTaskListOut(BaseModel):
    tasks: list[MeetingActionTaskOut]
