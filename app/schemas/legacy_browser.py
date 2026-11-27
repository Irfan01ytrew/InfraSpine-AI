"""Bounded approval-gated legacy-browser review contracts."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LegacyBrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=3, max_length=120)
    reason: str = Field(min_length=10, max_length=1000)
    start_url: str = Field(min_length=8, max_length=2048)
    link_labels: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("link_labels")
    @classmethod
    def unique_labels(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 100 for value in cleaned):
            raise ValueError("Link labels must contain 1 to 100 characters.")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("Link labels must be unique.")
        return cleaned


class LegacyBrowserRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requester_user_id: uuid.UUID | None
    approver_user_id: uuid.UUID | None
    label: str
    reason: str
    start_url: str
    link_labels: list[str]
    status: Literal["pending_approval", "running", "completed", "failed", "rejected", "cancelled"]
    attempts: int
    failure_code: str | None
    requested_at: datetime
    approved_at: datetime | None
    completed_at: datetime | None


class LegacyBrowserRunListOut(BaseModel):
    items: list[LegacyBrowserRunOut]


class CapturedBrowserPage(BaseModel):
    label: str
    url: str
    title: str
    text: str


class LegacyBrowserApprovalOut(BaseModel):
    run: LegacyBrowserRunOut
    pages: list[CapturedBrowserPage]
    request_count: int
    response_bytes: int


class LegacyBrowserStatusOut(BaseModel):
    enabled: bool
    maximum_pages: int = 4
    maximum_requests: int
    maximum_response_bytes: int
    maximum_result_characters: int
