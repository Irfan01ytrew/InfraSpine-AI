"""Validated contracts for department-scoped employee service requests."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequestCategory = Literal["it", "hr", "access", "facilities", "other", "purchase", "expense"]
RequestPriority = Literal["low", "normal", "high", "urgent"]
RequestStatus = Literal[
    "submitted", "acknowledged", "in_progress", "waiting_requester", "resolved", "rejected", "cancelled"
]


class ServiceRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: RequestCategory
    priority: RequestPriority = "normal"
    department_group_id: uuid.UUID | None = None
    subject: str = Field(..., min_length=1, max_length=160)
    # Required unless a custom form is used; then the answers fill it when left empty.
    description: str = Field(default="", max_length=8000)
    # Purchase requests and expense claims only; the currency defaults to the company currency.
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("9999999999999.99"), decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    form_id: uuid.UUID | None = None
    answers: dict[str, str | bool | float | int | list[str] | None] = Field(default_factory=dict, max_length=30)


class AttachmentOut(BaseModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    content_type: str


class FormAnswerOut(BaseModel):
    label: str
    value: str
    attachments: list[AttachmentOut] = Field(default_factory=list)


class ServiceRequestApproval(BaseModel):
    """What the current reviewer may do with a purchase request or expense claim."""

    can_approve: bool
    reason: str | None
    your_limit: Decimal | None
    limit_currency: str | None
    suggested_approver_email: str | None


class ApprovalStepOut(BaseModel):
    order: int
    label: str
    approver_kind: Literal["manager", "position", "person"]
    approver: str
    stage: int = 1
    stage_mode: Literal["all", "any"] = "all"
    status: Literal["waiting", "pending", "approved", "rejected", "cancelled"]
    final: bool
    decided_by_email: str | None
    decided_at: datetime | None
    comment: str | None
    on_behalf_of_email: str | None = None
    pending_since: datetime | None = None
    reminder_count: int = 0
    escalated_at: datetime | None = None


class RequestBudgetLine(BaseModel):
    name: str
    amount: Decimal
    currency: str
    approved: Decimal
    committed: Decimal
    after_approval: Decimal
    would_exceed: bool
    enforcement: Literal["warn", "block"]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=1500)


class ServiceRequestComment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comment: str = Field(..., min_length=1, max_length=2000)


class ServiceRequestTriage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["acknowledged", "in_progress", "waiting_requester", "resolved", "rejected"]
    assigned_to_user_id: uuid.UUID | None = None
    comment: str | None = Field(default=None, max_length=2000)


class ServiceRequestEventOut(BaseModel):
    id: int
    actor_email: str
    action: Literal["submitted", "status_changed", "commented", "cancelled", "sla_escalated"]
    from_status: RequestStatus | None
    to_status: RequestStatus
    comment: str | None
    created_at: datetime


class ServiceRequestOut(BaseModel):
    id: uuid.UUID
    requester_user_id: uuid.UUID
    requester_email: str
    department_group_id: uuid.UUID | None
    department_name: str | None
    assigned_to_user_id: uuid.UUID | None
    assigned_to_email: str | None
    category: RequestCategory
    priority: RequestPriority
    subject: str
    description: str
    status: RequestStatus
    amount: Decimal | None = None
    currency: str | None = None
    approval: ServiceRequestApproval | None = None
    # Multi-step approval: the request's steps, and whether the current user can decide the pending one.
    approval_steps: list[ApprovalStepOut] = Field(default_factory=list)
    can_decide_step: bool = False
    step_blocker: str | None = None
    # Budgets this request counts against; shown to the people who review or approve it.
    budgets: list[RequestBudgetLine] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    response_due_at: datetime
    response_sla_met_at: datetime | None
    sla_escalated_at: datetime | None
    response_sla_overdue: bool
    can_manage: bool
    can_cancel: bool
    events: list[ServiceRequestEventOut]
    form_name: str | None = None
    form_answers: list[FormAnswerOut] = Field(default_factory=list)


class ServiceRequestListResponse(BaseModel):
    items: list[ServiceRequestOut]


class ServiceRequestNotificationOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    notification_type: Literal["response_sla_escalated", "approval_step_pending", "approval_step_reminder", "approval_step_escalated"]
    created_at: datetime
    read_at: datetime | None


class ServiceRequestNotificationListResponse(BaseModel):
    items: list[ServiceRequestNotificationOut]
