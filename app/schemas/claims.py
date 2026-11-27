import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ClaimKind = Literal["proposal", "correction"]
ClaimStatus = Literal["submitted", "withdrawn"]
Category = Literal["policy", "financial", "legal", "client", "operational", "general"]


class DocumentSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["document"]
    document_id: uuid.UUID


class UrlSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["url"]
    url: str = Field(..., min_length=8, max_length=1024)


class NoteSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["note"]
    note: str = Field(..., min_length=1, max_length=500)


SourceIn = Annotated[DocumentSourceIn | UrlSourceIn | NoteSourceIn, Field(discriminator="kind")]


class DocumentSourceOut(BaseModel):
    kind: Literal["document"]
    document_id: uuid.UUID
    filename: str


class UrlSourceOut(BaseModel):
    kind: Literal["url"]
    url: str


class NoteSourceOut(BaseModel):
    kind: Literal["note"]
    note: str


SourceOut = Annotated[DocumentSourceOut | UrlSourceOut | NoteSourceOut, Field(discriminator="kind")]


class ClaimEventOut(BaseModel):
    id: int
    action: Literal["submitted", "withdrawn"]
    from_status: ClaimStatus | None
    to_status: ClaimStatus
    created_at: datetime


class ClaimOut(BaseModel):
    id: uuid.UUID
    kind: ClaimKind
    statement: str
    category: Category
    scope_group_id: uuid.UUID | None
    sources: list[SourceOut]
    status: ClaimStatus
    created_at: datetime
    withdrawn_at: datetime | None
    verified: Literal[False] = False  # WP3 never publishes or verifies a claim


class ClaimDetail(ClaimOut):
    history: list[ClaimEventOut]


class ClaimListResponse(BaseModel):
    items: list[ClaimOut]


class CreateClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: ClaimKind
    statement: str = Field(..., min_length=1, max_length=4000)
    category: Category
    scope_group_id: uuid.UUID | None = None
    sources: list[SourceIn] = Field(default_factory=list, max_length=20)
