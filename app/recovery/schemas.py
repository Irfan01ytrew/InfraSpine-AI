from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Source = Literal["agent", "workflow", "api", "webhook", "worker", "database", "infrastructure", "approval", "policy", "supplier", "process"]
Action = Literal["probe", "retry", "resume", "reprocess", "fallback", "restart", "compensate", "route_approver"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TenantInput(Input):
    name: str = Field(min_length=2, max_length=120)


class MemberInput(Input):
    user_id: UUID
    role: Literal["owner", "responder", "observer"]
    active: bool = True


class TargetInput(Input):
    name: str = Field(min_length=2, max_length=120)
    owner_id: UUID
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    resource: str = Field(min_length=1, max_length=160)
    threshold: int = Field(default=1, ge=1, le=100)
    window_seconds: int = Field(default=300, ge=10, le=86400)
    sla_seconds: int = Field(default=3600, ge=30, le=604800)
    check_interval: int = Field(default=60, ge=10, le=86400)


class EventInput(Input):
    target_id: UUID
    event_key: str = Field(min_length=1, max_length=120)
    source: Source
    category: Literal["timeout", "unavailable", "crash", "stalled", "conflict", "permission", "policy", "sla", "business", "anomaly"]
    error_code: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,80}$")
    title: str = Field(min_length=3, max_length=200)
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    condition: Literal["failure", "transient", "expected_rejection"] = "failure"
    occurred_at: datetime
    correlation_key: str | None = Field(default=None, max_length=120)
    evidence: dict = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("An explicit timezone is required")
        return value


class PlaybookInput(Input):
    name: str = Field(min_length=3, max_length=120)
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    action: Action
    preapproved: bool = False
    max_attempts: int = Field(default=3, ge=1, le=5)
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    backoff_seconds: int = Field(default=5, ge=1, le=300)
    verify_samples: int = Field(default=2, ge=2, le=5)


class TransitionInput(Input):
    version: int = Field(ge=1)
    state: Literal["triaged", "investigating", "escalated", "cancelled", "resolved"]
    reason: str = Field(min_length=3, max_length=2000)
    disposition: Literal["false_positive", "accepted_risk", "duplicate"] | None = None
    assignee_id: UUID | None = None


class ProposalInput(Input):
    version: int = Field(ge=1)
    playbook_id: UUID
    request_key: str = Field(min_length=1, max_length=120)


class DecisionInput(Input):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve: bool
    reason: str = Field(min_length=3, max_length=2000)


class NoteInput(Input):
    note: str = Field(min_length=3, max_length=4000)
    kind: Literal["evidence", "postmortem", "preventive_action"] = "evidence"


class ToggleInput(Input):
    enabled: bool
