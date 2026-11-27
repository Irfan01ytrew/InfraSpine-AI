"""Bounded API shapes for employee knowledge interviews and SOP review."""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InterviewQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(..., min_length=2, max_length=40)
    prompt: str = Field(..., min_length=3, max_length=300)
    required: bool = True

    @field_validator("key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", value):
            raise ValueError("Question keys must start with a lowercase letter and use lowercase letters, digits or underscores.")
        return value


class InterviewTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(..., min_length=3, max_length=80)
    title: str = Field(..., min_length=1, max_length=160)
    description: str = Field(..., min_length=1, max_length=800)
    questions: list[InterviewQuestion] = Field(..., min_length=1, max_length=20)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
            raise ValueError("Use lowercase letters, digits and single hyphens in the template key.")
        return normalized

    @model_validator(mode="after")
    def unique_question_keys(self):
        keys = [question.key for question in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("Question keys must be unique.")
        return self


class InterviewTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(None, min_length=1, max_length=160)
    description: str | None = Field(None, min_length=1, max_length=800)
    questions: list[InterviewQuestion] | None = Field(None, min_length=1, max_length=20)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_questions(self):
        if self.questions is not None:
            keys = [question.key for question in self.questions]
            if len(keys) != len(set(keys)):
                raise ValueError("Question keys must be unique.")
        if not self.model_fields_set:
            raise ValueError("Change at least one template field.")
        return self


class InterviewTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    description: str
    questions: list[InterviewQuestion]
    is_active: bool
    built_in: bool


class InterviewScopeOut(BaseModel):
    id: uuid.UUID
    name: str


class InterviewOptionsOut(BaseModel):
    templates: list[InterviewTemplateOut]
    managed_templates: list[InterviewTemplateOut] = Field(default_factory=list)
    departments: list[InterviewScopeOut]
    can_manage_templates: bool


class InterviewSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    department_group_id: uuid.UUID | None = None


class InterviewProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answers: dict[str, str] = Field(..., max_length=20)
    sop_title: str | None = Field(None, max_length=180)


class InterviewReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approved", "changes_requested"]
    rationale: str = Field(..., min_length=3, max_length=2000)


class InterviewReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    revision_number: int
    reviewer_user_id: uuid.UUID
    reviewer_email: str
    decision: Literal["approved", "changes_requested"]
    rationale: str
    reviewed_at: datetime


class InterviewSopRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    revision_number: int
    title: str
    body: str
    answers_snapshot: dict[str, str]
    submitted_by_user_id: uuid.UUID
    created_at: datetime


class InterviewSessionOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    template_title: str
    template_questions: list[InterviewQuestion]
    owner_user_id: uuid.UUID
    owner_email: str
    department_group_id: uuid.UUID | None
    department_name: str | None
    status: Literal["in_progress", "submitted", "changes_requested", "approved", "withdrawn"]
    answers: dict[str, str]
    sop_title: str
    sop_body: str
    revision_number: int
    submitted_revision: int | None
    submitted_at: datetime | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_edit: bool
    can_review: bool
    reviews: list[InterviewReviewOut]
    sop_revisions: list[InterviewSopRevisionOut]


class InterviewSessionListOut(BaseModel):
    sessions: list[InterviewSessionOut]

