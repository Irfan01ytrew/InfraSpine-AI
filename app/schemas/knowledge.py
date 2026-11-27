import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["policy", "financial", "legal", "client", "operational", "general"]
Visibility = Literal["private", "restricted", "shared"]
RevisionStatus = Literal["draft", "submitted", "published", "superseded", "withdrawn"]
OrphanReason = Literal["document_revision_changed", "source_deleted", "passage_removed"]


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: uuid.UUID
    quote: str = Field(..., min_length=1, max_length=2000)


class EvidenceOut(BaseModel):
    id: int
    document_id: uuid.UUID | None
    document_key: uuid.UUID
    document_version: int
    filename: str
    chunk_key: int
    page_number: int
    start_offset: int
    end_offset: int
    quote: str
    prefix: str
    suffix: str
    orphan_reason: OrphanReason | None
    orphaned_at: datetime | None


class RevisionOut(BaseModel):
    id: uuid.UUID
    revision_number: int
    statement: str
    category: Category
    visibility: Visibility
    scope_group_id: uuid.UUID | None
    status: RevisionStatus
    created_at: datetime
    published: bool
    evidence: list[EvidenceOut]


class KnowledgeOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    current: RevisionOut


class KnowledgeDetail(BaseModel):
    id: uuid.UUID
    created_at: datetime
    revisions: list[RevisionOut]


class KnowledgeListResponse(BaseModel):
    items: list[KnowledgeOut]


class CreateKnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    statement: str = Field(..., min_length=1, max_length=4000)
    category: Category
    visibility: Visibility = "private"
    scope_group_id: uuid.UUID | None = None
    evidence: list[EvidenceIn] = Field(..., min_length=1, max_length=10)
