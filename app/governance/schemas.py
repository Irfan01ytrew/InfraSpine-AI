"""Bounded policy language and HTTP inputs. No executable expressions or caller risk labels."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ACTIONS = {"document.metadata": ("document", "low"), "document.share": ("document", "critical"), "agent.shutdown": ("agent", "high")}
PERMISSIONS = {"security.read", "security.manage", "security.audit", "approval.decide", *ACTIONS}
RISKS = ("low", "medium", "high", "critical")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TenantInput(Input):
    name: str = Field(min_length=2, max_length=160)


class DepartmentInput(Input):
    name: str = Field(min_length=2, max_length=120)
    parent_id: UUID | None = None


class MemberInput(Input):
    email: str = Field(min_length=3, max_length=320)
    department_id: UUID | None = None
    position_title: str = Field(default="", max_length=120)
    all_departments: bool = False
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def aware_date(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Include a timezone in the expiry.")
        return value


class RoleInput(Input):
    name: str = Field(min_length=2, max_length=120)
    permissions: list[str] = Field(max_length=20)

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, values):
        if not set(values) <= PERMISSIONS or len(values) != len(set(values)):
            raise ValueError("Permissions must be unique catalog entries.")
        return sorted(values)


class AssignmentInput(Input):
    user_id: UUID
    role_id: UUID
    expires_at: datetime | None = None
    _aware_date = field_validator("expires_at")(MemberInput.aware_date.__func__)


class Rule(Input):
    name: str = Field(min_length=2, max_length=100)
    effect: Literal["allow", "deny", "approval"]
    actions: list[Literal["document.metadata", "document.share", "agent.shutdown"]] = Field(min_length=1, max_length=3)
    role_ids: list[UUID] = Field(default_factory=list, max_length=30)
    department_ids: list[UUID] = Field(default_factory=list, max_length=30)
    resource_ids: list[UUID] = Field(default_factory=list, max_length=100)
    classifications: list[Literal["internal", "confidential", "restricted"]] = Field(default_factory=list, max_length=3)
    risk: Literal["low", "medium", "high", "critical"] = "low"


class PolicyInput(Input):
    rules: list[Rule] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_names(self):
        if len({r.name for r in self.rules}) != len(self.rules):
            raise ValueError("Rule names must be unique.")
        return self


class ResourceInput(Input):
    kind: Literal["document", "agent"]
    external_id: UUID
    department_id: UUID | None = None
    classification: Literal["internal", "confidential", "restricted"] = "internal"


class AgentInput(Input):
    name: str = Field(min_length=2, max_length=120)
    owner_user_id: UUID
    tools: list[Literal["document.metadata", "document.share", "agent.shutdown"]] = Field(max_length=3)
    max_calls: int = Field(default=20, ge=1, le=1000)
    window_seconds: int = Field(default=3600, ge=60, le=86400)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class ActionInput(Input):
    resource_id: UUID
    action: Literal["document.metadata", "document.share", "agent.shutdown"]
    agent_id: UUID | None = None


class SimulationInput(ActionInput):
    subject_user_id: UUID
    version: int | None = Field(default=None, ge=1)


class DecisionInput(Input):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=1000)
