"""Bounded, version-one ingestion and review contracts. Unknown fields fail closed."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TenantInput(Input):
    name: Label


class MemberInput(Input):
    user_id: UUID
    role: Literal["viewer", "analyst", "steward"]


class ProcessInput(Input):
    name: Label
    description: str = Field(default="", max_length=2000)


class SourceInput(Input):
    name: Label
    kind: Literal["erp", "crm", "hrms", "accounting", "email", "document", "ticket", "workflow", "api", "event_log"]
    document_id: UUID | None = None
    retention_days: int = Field(default=365, ge=1, le=3650)


class GrantInput(Input):
    user_id: UUID
    allowed: bool


class EventInput(Input):
    external_id: Label
    case_id: Label
    activity: Label
    occurred_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    actor: Label | None = None
    department: Label | None = None
    outcome: str | None = Field(default=None, max_length=80)
    terminal: bool = False
    evidence_ref: str = Field(min_length=1, max_length=320)

    @model_validator(mode="after")
    def chronology(self):
        if self.finished_at and self.finished_at < self.occurred_at:
            raise ValueError("finished_at must be at or after occurred_at")
        return self


class BatchInput(Input):
    schema_version: Literal[1] = 1
    expected_cursor: str | None = Field(default=None, max_length=256)
    next_cursor: str | None = Field(default=None, max_length=256)
    events: list[EventInput] = Field(min_length=1, max_length=2000)


class BaselineInput(Input):
    source_id: UUID
    title: Label
    sequences: list[list[Label]] = Field(min_length=1, max_length=20)
    sla_hours: float = Field(gt=0, le=87600, allow_inf_nan=False)
    evidence_ref: str = Field(min_length=1, max_length=320)

    @model_validator(mode="after")
    def bounded_sequences(self):
        if any(not seq or len(seq) > 100 for seq in self.sequences):
            raise ValueError("Each approved variant must have 1 to 100 activities")
        return self


class ReviewInput(Input):
    decision: Literal["approved", "rejected"]


class SimulationInput(Input):
    model_id: UUID
    actor: Label
    absence_hours: float = Field(gt=0, le=8760, allow_inf_nan=False)
    replacement_capacity: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)


class ExtractionInput(Input):
    source_id: UUID


class ModelReviewInput(Input):
    decision: Literal["validated", "rejected"]


class RetentionInput(Input):
    confirm_source_id: UUID


class AnalysisQuestion(Input):
    model_id: UUID
    question: str = Field(min_length=3, max_length=2000)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: str
    attempts: int
    model_id: UUID | None
    error_code: str | None
    created_at: datetime
