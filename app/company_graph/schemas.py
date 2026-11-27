"""Version 1 input contracts. Untrusted data never becomes approved graph state directly."""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Kind = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Evidence(Input):
    source: str = Field(min_length=1, max_length=160)
    reference: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=2000)
    observed_at: AwareDatetime


class RecordInput(Input):
    record_type: Literal["entity", "relationship"]
    external_key: str = Field(min_length=1, max_length=220)
    kind: Kind
    name: str = Field(min_length=1, max_length=240)
    source_id: UUID | None = None
    target_id: UUID | None = None
    department_id: UUID | None = None
    visibility: Literal["tenant", "department", "restricted"] = "restricted"
    readers: list[UUID] = Field(default_factory=list, max_length=100)
    attributes: dict = Field(default_factory=dict)
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    confidence: float = Field(default=1, ge=0, le=1)
    inferred: bool = False
    retired: bool = False
    valid_from: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: AwareDatetime | None = None
    observed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_shape(self):
        if self.record_type == "entity" and (self.source_id or self.target_id):
            raise ValueError("Entities cannot have relationship endpoints.")
        if self.record_type == "relationship" and (not self.source_id or not self.target_id or self.source_id == self.target_id):
            raise ValueError("Relationships require two different entity endpoints.")
        if self.visibility == "department" and not self.department_id:
            raise ValueError("Department visibility requires a department.")
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("Validity end must follow the start.")
        if len(json.dumps(self.attributes, allow_nan=False)) > 12000:
            raise ValueError("Metadata is limited to 12,000 characters.")
        if self.observed_at > datetime.now(UTC):
            raise ValueError("Observation time cannot be in the future.")
        if any(e.observed_at > datetime.now(UTC) for e in self.evidence):
            raise ValueError("Evidence observation time cannot be in the future.")
        return self


class RecordProposal(Input):
    record_id: UUID | None = None
    base_version: int = Field(default=0, ge=0)
    record: RecordInput


class MemberInput(Input):
    user_id: UUID
    role: Literal["owner", "approver", "editor", "viewer"]
    departments: list[UUID] = Field(default_factory=list, max_length=100)
    all_departments: bool = False
    active: bool = True
    base_version: int = Field(default=0, ge=0)


class Decision(Input):
    approve: bool
    reason: str = Field(min_length=3, max_length=1000)


class TenantInput(Input):
    name: str = Field(min_length=1, max_length=160)


class SourceInput(Input):
    name: str = Field(min_length=1, max_length=160)
    kind: Kind
    stale_after_days: int = Field(default=90, ge=1, le=3650)


class SyncItem(Input):
    key: str = Field(min_length=1, max_length=120)
    kind: Kind
    name: str = Field(min_length=1, max_length=240)
    attributes: dict = Field(default_factory=dict)
    quote: str = Field(min_length=1, max_length=2000)
    source_key: str | None = Field(default=None, max_length=120)
    target_key: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=1, ge=0, le=1)
    inferred: bool = False


class SyncInput(Input):
    idempotency_key: str = Field(min_length=1, max_length=120)
    observed_at: AwareDatetime
    reference: str = Field(min_length=1, max_length=500)
    visibility: Literal["tenant", "department", "restricted"] = "restricted"
    department_id: UUID | None = None
    readers: list[UUID] = Field(default_factory=list, max_length=100)
    items: list[SyncItem] = Field(min_length=1, max_length=500)


class Simulation(Input):
    failed_ids: list[UUID] = Field(min_length=1, max_length=20)
    scenario: Literal["resignation", "absence", "system_outage", "supplier_failure", "restructuring"] = "system_outage"
    depth: int = Field(default=6, ge=1, le=12)
    as_of: AwareDatetime | None = None


class GraphQuestion(Input):
    question: str = Field(min_length=1, max_length=1000)


class ExtractionInput(Input):
    text: str = Field(min_length=10, max_length=20000)
    reference: str = Field(min_length=1, max_length=500)
