"""Strict request/response schemas for the bounded internal workflow engine."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowDefinitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    department_group_id: uuid.UUID
    trigger_type: Literal["manual", "interval", "service_request", "request_sla"] = "manual"
    interval_minutes: int | None = Field(default=None, ge=60, le=10080)
    action_type: Literal["create_task", "notify_member", "assign_personal_task"] = "create_task"
    assignee_user_id: uuid.UUID | None = None
    task_title: str | None = Field(default=None, min_length=1, max_length=180)
    task_body: str | None = Field(default=None, min_length=1, max_length=4000)
    due_days: int | None = Field(default=None, ge=0, le=365)
    notification_recipient_user_id: uuid.UUID | None = None
    notification_title: str | None = Field(default=None, min_length=1, max_length=180)
    notification_body: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action_type in {"create_task", "assign_personal_task"}:
            if self.assignee_user_id is None or self.task_title is None or self.task_body is None:
                raise ValueError("Task workflows require an assignee, title and instructions.")
            if self.notification_recipient_user_id or self.notification_title or self.notification_body:
                raise ValueError("Task workflows cannot include notification fields.")
        else:
            if self.notification_recipient_user_id is None or self.notification_title is None or self.notification_body is None:
                raise ValueError("Member notifications require a recipient, title and message.")
            if self.assignee_user_id or self.task_title or self.task_body or self.due_days is not None:
                raise ValueError("Member notifications cannot include task fields.")
        return self


class WorkflowVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=120)
    trigger_type: Literal["manual", "interval", "service_request", "request_sla"]
    interval_minutes: int | None = Field(default=None, ge=60, le=10080)
    action_type: Literal["create_task", "notify_member", "assign_personal_task"] = "create_task"
    assignee_user_id: uuid.UUID | None = None
    task_title: str | None = Field(default=None, min_length=1, max_length=180)
    task_body: str | None = Field(default=None, min_length=1, max_length=4000)
    due_days: int | None = Field(default=None, ge=0, le=365)
    notification_recipient_user_id: uuid.UUID | None = None
    notification_title: str | None = Field(default=None, min_length=1, max_length=180)
    notification_body: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action_type in {"create_task", "assign_personal_task"}:
            if self.assignee_user_id is None or self.task_title is None or self.task_body is None:
                raise ValueError("Task workflows require an assignee, title and instructions.")
            if self.notification_recipient_user_id or self.notification_title or self.notification_body:
                raise ValueError("Task workflows cannot include notification fields.")
        else:
            if self.notification_recipient_user_id is None or self.notification_title is None or self.notification_body is None:
                raise ValueError("Member notifications require a recipient, title and message.")
            if self.assignee_user_id or self.task_title or self.task_body or self.due_days is not None:
                raise ValueError("Member notifications cannot include task fields.")
        return self


class WorkflowOut(BaseModel):
    id: uuid.UUID
    definition_key: uuid.UUID
    version: int
    name: str
    department_group_id: uuid.UUID
    department_name: str
    trigger_type: Literal["manual", "interval", "service_request", "request_sla"]
    interval_minutes: int | None
    action_type: Literal["create_task", "notify_member", "assign_personal_task"]
    assignee_user_id: uuid.UUID | None
    task_title: str | None
    task_body: str | None
    due_days: int | None
    notification_recipient_user_id: uuid.UUID | None
    notification_title: str | None
    notification_body: str | None
    enabled: bool
    next_run_at: datetime | None
    created_at: datetime
    can_manage: bool


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowOut]


class WorkflowDryRunNodeOut(BaseModel):
    id: str
    node_type: Literal["trigger", "approval", "action", "completion"]
    title: str
    description: str


class WorkflowDryRunEdgeOut(BaseModel):
    source: str
    target: str


class WorkflowDryRunCheckOut(BaseModel):
    code: Literal["department_access", "active_assignee", "approval_gate", "allowlisted_action"]
    passed: bool
    summary: str


class WorkflowDryRunTaskPreview(BaseModel):
    title: str
    body: str
    assignee_email: str
    due_days: int | None


class WorkflowDryRunNotificationPreview(BaseModel):
    title: str
    body: str
    recipient_email: str


class WorkflowDryRunOut(BaseModel):
    workflow_id: uuid.UUID
    workflow_version: int
    valid: bool
    side_effects_performed: Literal[False] = False
    nodes: list[WorkflowDryRunNodeOut]
    edges: list[WorkflowDryRunEdgeOut]
    checks: list[WorkflowDryRunCheckOut]
    task_preview: WorkflowDryRunTaskPreview | None = None
    notification_preview: WorkflowDryRunNotificationPreview | None = None


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: uuid.UUID = Field(default_factory=uuid.uuid4)


class WorkflowDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approved: bool
    rationale: str = Field(..., min_length=3, max_length=2000)


class WorkflowRunOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_name: str
    requested_by_user_id: uuid.UUID | None
    department_group_id: uuid.UUID
    status: Literal[
        "pending_approval", "queued", "processing", "completed", "rejected", "cancelled", "failed"
    ]
    attempts: int
    last_error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    approval_decision: Literal["approved", "rejected"] | None
    approver_user_id: uuid.UUID | None
    approval_rationale: str | None
    approval_decided_at: datetime | None
    can_approve: bool
    can_cancel: bool
    can_retry: bool


class WorkflowRunListResponse(BaseModel):
    runs: list[WorkflowRunOut]


class WorkflowTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    assignee_user_id: uuid.UUID
    department_group_id: uuid.UUID
    title: str
    body: str
    due_at: datetime | None
    status: Literal["open", "completed"]
    created_at: datetime
    completed_at: datetime | None


class WorkflowTaskListResponse(BaseModel):
    tasks: list[WorkflowTaskOut]


class WorkflowNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID | None
    notification_type: Literal["run_approved", "run_rejected", "task_assigned", "approval_required", "workflow_message"]
    message_title: str | None
    message_body: str | None
    created_at: datetime
    read_at: datetime | None


class WorkflowNotificationListResponse(BaseModel):
    items: list[WorkflowNotificationOut]
