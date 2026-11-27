"""Validated API contracts for source-grounded document obligations and version diffs."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class DocumentObligationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    rationale: str = Field(min_length=3, max_length=2000)
    source_quote: str = Field(min_length=8, max_length=1200)
    assigned_to_email: str | None = Field(default=None, max_length=320)
    due_at: datetime | None = None

    @field_validator("title", "rationale", "source_quote")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This field must not be blank.")
        return value

    @field_validator("assigned_to_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None


class DocumentObligationUpdate(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    status: Literal["open", "acknowledged", "completed"] | None = None
    assigned_to_email: str | None = Field(default=None, max_length=320)
    due_at: datetime | None = None
    rationale: str | None = Field(default=None, min_length=3, max_length=2000)

    @field_validator("assigned_to_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None

    @field_validator("rationale")
    @classmethod
    def clean_rationale(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("A reason is required for an obligation change.")
        return value


class DocumentObligationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_user_id: uuid.UUID | None
    action: str
    from_status: str | None
    to_status: str | None
    rationale: str | None
    details: dict
    created_at: datetime


class DocumentObligationOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_filename: str
    source_version: int
    source_sha256: str
    source_quote: str
    source_page_number: int
    source_locator: dict
    source_current: bool
    title: str
    rationale: str
    assigned_to_user_id: uuid.UUID
    assigned_to_email: str
    created_by_user_id: uuid.UUID
    due_at: datetime | None
    status: Literal["open", "acknowledged", "completed"]
    version: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_manage: bool
    can_update_status: bool
    history: list[DocumentObligationEventOut]


class DocumentObligationListOut(BaseModel):
    obligations: list[DocumentObligationOut]


class DocumentDiffLine(BaseModel):
    kind: Literal["added", "removed"]
    text: str = Field(max_length=1200)


class DocumentVersionDiffOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    from_version: int
    to_version: int
    from_sha256: str
    to_sha256: str
    method: Literal["deterministic_text_diff"]
    added_lines: int
    removed_lines: int
    changes: list[DocumentDiffLine]
    truncated: bool
