"""Positions, reporting lines and the organization chart."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobFamily = Literal[
    "executive", "management", "professional", "technical", "operations",
    "administration", "governance", "sales", "frontline", "external", "other",
]


class PositionOut(BaseModel):
    id: uuid.UUID
    title: str
    job_family: JobFamily
    level: int | None
    description: str
    is_active: bool
    holders: int
    created_at: datetime
    updated_at: datetime


class PositionListResponse(BaseModel):
    positions: list[PositionOut]


class CreatePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=2, max_length=120)
    job_family: JobFamily = "other"
    level: int | None = Field(default=None, ge=1, le=20)
    description: str = Field(default="", max_length=1000)


class UpdatePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=2, max_length=120)
    job_family: JobFamily | None = None
    level: int | None = Field(default=None, ge=1, le=20)
    clear_level: bool = False
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None


class LibraryPositionOut(BaseModel):
    title: str
    job_family: JobFamily
    level: int | None
    description: str
    added: bool


class PositionLibraryResponse(BaseModel):
    positions: list[LibraryPositionOut]


class ApplyLibraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None adds every library position that does not exist yet.
    titles: list[str] | None = Field(default=None, max_length=200)


class ApplyLibraryResponse(BaseModel):
    created: list[str]
    skipped: list[str]


class OrgAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None


class OrgHistoryEntry(BaseModel):
    position_title: str | None
    reports_to_email: str | None
    started_at: datetime
    ended_at: datetime | None


class OrgHistoryResponse(BaseModel):
    entries: list[OrgHistoryEntry]


class OrgChartNode(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    position_id: uuid.UUID | None
    position_title: str | None
    job_family: JobFamily | None
    reports_to_id: uuid.UUID | None
    departments: list[str]


class OrgChartResponse(BaseModel):
    people: list[OrgChartNode]


class ApprovalLimitOut(BaseModel):
    id: uuid.UUID
    category: Literal["purchase", "expense"]
    position_id: uuid.UUID | None
    position_title: str | None
    user_id: uuid.UUID | None
    user_email: str | None
    max_amount: Decimal
    currency: str


class ApprovalLimitListResponse(BaseModel):
    limits: list[ApprovalLimitOut]


class CreateApprovalLimitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["purchase", "expense"]
    position_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    max_amount: Decimal = Field(..., ge=0, le=Decimal("9999999999999.99"), decimal_places=2)
    currency: str = Field(..., pattern=r"^[A-Z]{3}$")


class UpdateApprovalLimitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("9999999999999.99"), decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    is_active: bool | None = None


class ApprovalChainStepOut(BaseModel):
    id: uuid.UUID
    category: Literal["purchase", "expense"]
    step_order: int
    label: str
    approver_kind: Literal["manager", "position", "person"]
    position_id: uuid.UUID | None
    position_title: str | None
    user_id: uuid.UUID | None
    user_email: str | None
    min_amount: Decimal | None
    parallel_with_previous: bool = False
    stage_mode: Literal["all", "any"] = "all"


class ApprovalChainResponse(BaseModel):
    steps: list[ApprovalChainStepOut]


class AddApprovalChainStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: Literal["purchase", "expense"]
    label: str = Field(..., min_length=2, max_length=80)
    approver_kind: Literal["manager", "position", "person"]
    position_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    min_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("9999999999999.99"), decimal_places=2)
    parallel_with_previous: bool = False
    stage_mode: Literal["all", "any"] = "all"


class UpdateApprovalChainStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parallel_with_previous: bool | None = None
    stage_mode: Literal["all", "any"] | None = None


class MoveApprovalChainStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal[-1, 1]


class BudgetOut(BaseModel):
    id: uuid.UUID
    name: str
    department_group_id: uuid.UUID
    department_name: str
    category: Literal["purchase", "expense", "all"]
    period_start: date
    period_end: date
    amount: Decimal
    currency: str
    enforcement: Literal["warn", "block"]
    approved: Decimal
    committed: Decimal
    remaining: Decimal


class BudgetListResponse(BaseModel):
    budgets: list[BudgetOut]


class CreateBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=2, max_length=120)
    department_group_id: uuid.UUID
    category: Literal["purchase", "expense", "all"] = "all"
    period_start: date
    period_end: date
    amount: Decimal = Field(..., gt=0, le=Decimal("9999999999999.99"), decimal_places=2)
    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    enforcement: Literal["warn", "block"] = "warn"


class UpdateBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("9999999999999.99"), decimal_places=2)
    enforcement: Literal["warn", "block"] | None = None
    is_active: bool | None = None


class DelegationOut(BaseModel):
    id: uuid.UUID
    delegator_user_id: uuid.UUID
    delegator_email: str
    delegate_user_id: uuid.UUID
    delegate_email: str
    starts_on: date
    ends_on: date
    reason: str | None
    current: bool


class DelegationListResponse(BaseModel):
    delegations: list[DelegationOut]


class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    delegate_user_id: uuid.UUID
    starts_on: date
    ends_on: date
    reason: str | None = Field(default=None, max_length=200)
    # Administrators only: set up delegation for someone else.
    delegator_user_id: uuid.UUID | None = None
