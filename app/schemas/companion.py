"""Pydantic schemas for the Companion OS API (Step AN, slices 2–3)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["low", "medium", "high"]
Language = Literal["en", "ur", "roman_urdu"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["active", "archived"] | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    created_at: datetime


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=180)
    priority: Priority = "medium"
    deadline: date | None = None
    project_id: uuid.UUID | None = None


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=180)
    priority: Priority | None = None
    deadline: date | None = None
    project_id: uuid.UUID | None = None
    status: Literal["active", "archived", "completed"] | None = None
    clear_deadline: bool = False
    clear_project: bool = False


class GoalOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    title: str
    status: str
    priority: str
    deadline: date | None
    created_at: datetime


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: uuid.UUID
    title: str = Field(min_length=1, max_length=180)
    priority: Priority = "medium"
    due: date | None = None
    estimated_hours: float = Field(default=0.0, ge=0)
    action_kind: Literal["internal", "send", "book", "pay", "share", "publish", "deploy"] = "internal"


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=180)
    priority: Priority | None = None
    due: date | None = None
    estimated_hours: float | None = Field(default=None, ge=0)
    action_kind: Literal["internal", "send", "book", "pay", "share", "publish", "deploy"] | None = None
    status: Literal["todo", "in_progress", "done", "cancelled"] | None = None
    clear_due: bool = False


class TaskOut(BaseModel):
    id: uuid.UUID
    goal_id: uuid.UUID
    title: str
    status: str
    priority: str
    due: date | None
    estimated_hours: float
    action_kind: str
    created_at: datetime


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    weekly_capacity_hours: float | None = Field(default=None, gt=0, le=168)
    quiet_start: time | None = None
    quiet_end: time | None = None
    retention_days: int | None = Field(default=None, ge=1)


class PreferencesOut(BaseModel):
    language: str
    timezone: str
    weekly_capacity_hours: float
    quiet_start: time
    quiet_end: time
    retention_days: int


class ProgressCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: uuid.UUID
    task_id: uuid.UUID | None = None
    percent: int = Field(ge=0, le=100)
    note: str = Field(default="", max_length=1000)
    on: date


class ProgressOut(BaseModel):
    id: uuid.UUID
    goal_id: uuid.UUID
    task_id: uuid.UUID | None
    percent: int
    note: str
    on: date
    created_at: datetime


class PlannedTaskOut(BaseModel):
    task_id: uuid.UUID
    title: str
    rank: int
    overdue_days: int
    needs_approval: bool
    reason: str
    due: date | None
    estimated_hours: float


class PlanOut(BaseModel):
    on: date
    tasks: list[PlannedTaskOut]
    overdue_count: int
    total_estimated_hours: float
    overloaded: bool
    warnings: list[str]


class GoalProgressOut(BaseModel):
    goal_id: uuid.UUID
    title: str
    tasks_total: int
    tasks_done: int
    percent: int
    last_progress_on: date | None
    stalled: bool


class BriefingOut(BaseModel):
    on: date
    plan: PlanOut
    goals: list[GoalProgressOut]
    stalled_goal_ids: list[uuid.UUID]
    leave_off: dict[str, dict]


class ConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str = Field(default="email_digest", max_length=32)
    scope: str = Field(default="daily_digest", min_length=1, max_length=64)
    expires_at: datetime | None = None


class ConsentOut(BaseModel):
    id: uuid.UUID
    integration: str
    scope: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    active: bool


class DigestQueueOut(BaseModel):
    id: uuid.UUID
    digest_date: date
    subject: str
    status: str
    approval_id: uuid.UUID
    action_hash: str


class DigestSendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: uuid.UUID


class DigestOut(BaseModel):
    id: uuid.UUID
    digest_date: date
    subject: str
    status: str
    approval_id: uuid.UUID | None
    message_id: str | None
    created_at: datetime
    sent_at: datetime | None


class DeleteAllOut(BaseModel):
    progress: int
    digests: int
    tasks: int
    goals: int
    projects: int
    consents: int
    preferences: int
