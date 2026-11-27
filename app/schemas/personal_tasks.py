"""Direct task assignment and My Work inbox schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PersonalTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=4000)
    assignee_user_id: UUID | None = None
    department_group_id: UUID | None = None
    due_at: datetime | None = None

    @field_validator("title", "body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("A due date must include a timezone.")
        return value


class PersonalTaskAssigneeOut(BaseModel):
    user_id: UUID
    email: str


class PersonalTaskDepartmentOut(BaseModel):
    id: UUID
    name: str
    members: list[PersonalTaskAssigneeOut]


class PersonalTaskOptionsOut(BaseModel):
    departments: list[PersonalTaskDepartmentOut]


class PersonalTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by_user_id: UUID
    assignee_user_id: UUID
    department_group_id: UUID | None
    title: str
    body: str
    due_at: datetime | None
    status: str
    created_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    created_by_email: str
    assignee_email: str


class PersonalTaskListOut(BaseModel):
    tasks: list[PersonalTaskOut]


class PersonalTaskReminderOut(BaseModel):
    id: UUID
    task_id: UUID
    title: str
    reminder_type: Literal["due_soon", "overdue"]
    due_at: datetime
    created_at: datetime
    read_at: datetime | None
    email_status: Literal["disabled", "pending", "sending", "sent", "retry", "failed", "cancelled"]


class PersonalTaskReminderListOut(BaseModel):
    reminders: list[PersonalTaskReminderOut]
