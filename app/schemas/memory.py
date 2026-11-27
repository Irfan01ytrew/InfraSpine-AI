import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryKind = Literal["preference", "terminology", "fact"]


class MemoryOut(BaseModel):
    id: uuid.UUID
    kind: MemoryKind
    subject: str | None
    content: str
    status: Literal["active", "superseded"]
    source: Literal["chat", "manual"]
    supersedes_id: uuid.UUID | None
    created_at: datetime
    superseded_at: datetime | None


class MemoryListResponse(BaseModel):
    items: list[MemoryOut]
    active_count: int
    max_items: int


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(..., min_length=1, max_length=500)
    subject: str | None = Field(default=None, max_length=100)
    kind: MemoryKind | None = None  # inferred from the content when omitted


class UpdateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str | None = Field(default=None, min_length=1, max_length=500)
    subject: str | None = Field(default=None, max_length=100)  # "" clears the subject
    kind: MemoryKind | None = None


class MemorySaveResponse(BaseModel):
    item: MemoryOut
    outcome: Literal["created", "duplicate", "superseded", "updated"]
    replaced_id: uuid.UUID | None


class MemoryClearResponse(BaseModel):
    deleted: int
