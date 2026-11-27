"""Validated contracts for persistent human review of finance extractions."""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class FinancialExtractionFieldOut(BaseModel):
    value: str | None
    raw: str | None
    valid: bool
    source: dict | None


class FinancialLineItemOut(BaseModel):
    description: str | None
    quantity: str | None
    unit_price: str | None
    amount: str | None
    currency: str | None
    source: dict


class FinancialFormExceptionOut(BaseModel):
    code: str
    field: str
    message: str
    severity: Literal["error", "warning"]
    source: dict | None


class FinancialFormExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["auto", "invoice", "purchase_order", "form"] = "auto"
    date_order: Literal["dmy", "mdy", "ymd"] = "dmy"


class FinancialFormReviewLineItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: StrictStr | None = Field(default=None, max_length=500)
    quantity: StrictStr | None = Field(default=None, max_length=40)
    unit_price: StrictStr | None = Field(default=None, max_length=40)
    amount: StrictStr | None = Field(default=None, max_length=40)
    currency: StrictStr | None = Field(default=None, max_length=3)

    @field_validator("description", "quantity", "unit_price", "amount", "currency")
    @classmethod
    def trim_values(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        return value or None


class FinancialFormReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_document_version: StrictInt = Field(ge=1)
    profile: Literal["invoice", "purchase_order", "form"]
    date_order: Literal["dmy", "mdy", "ymd"] = "dmy"
    fields: dict[str, StrictStr | None] = Field(max_length=100)
    line_items: list[FinancialFormReviewLineItemIn] = Field(max_length=2000)
    reviewed_exception_indexes: list[StrictInt] = Field(max_length=7000)
    rationale: StrictStr = Field(min_length=3, max_length=2000)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        normalized = {}
        for name, raw in value.items():
            if not re.fullmatch(r"[a-z0-9_]{1,80}", name):
                raise ValueError("Finance field names must use lowercase letters, numbers, and underscores.")
            clean = raw.strip() if raw is not None else None
            if clean is not None and len(clean) > 1000:
                raise ValueError("Finance field values are limited to 1,000 characters.")
            normalized[name] = clean or None
        return normalized

    @field_validator("reviewed_exception_indexes")
    @classmethod
    def unique_exception_indexes(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Review each extraction exception once.")
        return value

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Add a review reason with at least three characters.")
        return value


class FinancialFormReviewRevisionOut(BaseModel):
    revision: int
    reviewed_by_email: str
    reviewed_at: datetime
    rationale: str
    fields: dict[str, str | None]
    line_items: list[FinancialLineItemOut]
    reviewed_exception_indexes: list[int]


class FinancialExtractionOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    document_version: int
    profile: Literal["invoice", "purchase_order", "form"]
    date_order: Literal["dmy", "mdy", "ymd"]
    status: Literal["ready", "needs_review"]
    fields: dict[str, FinancialExtractionFieldOut]
    line_items: list[FinancialLineItemOut]
    exceptions: list[FinancialFormExceptionOut]
    note: str
    record_id: uuid.UUID | None = None
    review_status: Literal["not_saved", "pending", "reviewed"] = "not_saved"
    review: FinancialFormReviewRevisionOut | None = None
    review_history: list[FinancialFormReviewRevisionOut] = Field(default_factory=list, max_length=20)


class FinancialFormReviewQueueItem(BaseModel):
    record_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    document_version: int
    profile: Literal["invoice", "purchase_order", "form"]
    extraction_status: Literal["ready", "needs_review"]
    exception_count: int = Field(ge=0, le=7000)
    created_at: datetime
    can_review: bool


class FinancialFormReviewQueueOut(BaseModel):
    records: list[FinancialFormReviewQueueItem] = Field(max_length=500)
