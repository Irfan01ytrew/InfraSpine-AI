"""Schemas for controlled agent actions, human approvals, and idempotent execution."""

from datetime import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["webhook_dispatch", "document_publish", "notification_broadcast"]
ActionStatus = Literal["pending_approval", "approved", "rejected", "expired", "executed"]


class ActionProposalCreate(BaseModel):
    agent_profile_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    action_type: ActionType
    parameters: dict
    rationale: str = Field(min_length=3, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    ttl_hours: int = Field(default=24, ge=1, le=168)


class ActionProposalOut(BaseModel):
    id: uuid.UUID
    agent_profile_id: uuid.UUID | None
    requester_user_id: uuid.UUID | None
    department_id: uuid.UUID | None
    action_type: str
    parameters: dict
    rationale: str
    idempotency_key: str
    status: str
    approver_user_id: uuid.UUID | None
    approval_rationale: str | None
    requested_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None
    executed_at: datetime | None
    execution_result: dict | None
    is_expired: bool
    can_approve: bool


class ActionProposalListResponse(BaseModel):
    proposals: list[ActionProposalOut]
    total: int


class ActionDecisionRequest(BaseModel):
    rationale: str = Field(min_length=3, max_length=1000)


class ActionExecutionResponse(BaseModel):
    proposal_id: uuid.UUID
    status: str
    action_type: str
    executed_at: datetime
    execution_result: dict
