"""Typed contracts for the reviewed internal agent-template catalog."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agents import AgentTool, AgentType, KnowledgeScope


class CatalogDefinitionFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    summary: str = Field(..., min_length=1, max_length=2000)
    agent_type: AgentType
    knowledge_scope: KnowledgeScope = "requester_acl"
    tool_policy: list[AgentTool] = Field(..., min_length=1, max_length=4)


class CatalogTemplateCreateRequest(CatalogDefinitionFields):
    slug: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")


class CatalogVersionRequest(CatalogDefinitionFields):
    pass


class CatalogReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    rationale: str = Field(..., min_length=3, max_length=2000)


class CatalogRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rationale: str = Field(..., min_length=3, max_length=2000)


class CatalogGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=320)
    permission: Literal["publish", "review"]


class CatalogTemplateOut(BaseModel):
    id: uuid.UUID
    slug: str
    version: int
    name: str
    summary: str
    agent_type: AgentType
    knowledge_scope: KnowledgeScope
    tool_policy: list[AgentTool]
    published_at: datetime
    can_instantiate: bool


class CatalogPublicListResponse(BaseModel):
    items: list[CatalogTemplateOut]
    can_publish: bool
    can_review: bool


class CatalogVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    name: str
    summary: str
    agent_type: AgentType
    knowledge_scope: KnowledgeScope
    tool_policy: list[AgentTool]
    status: Literal["draft", "pending_review", "approved", "rejected", "superseded"]
    author_email: str
    created_at: datetime
    submitted_by_email: str | None
    submitted_at: datetime | None
    reviewed_by_email: str | None
    reviewed_at: datetime | None
    review_rationale: str | None


class CatalogManagedTemplateOut(BaseModel):
    id: uuid.UUID
    slug: str
    latest_version: int
    published_version: int | None
    created_by_email: str
    created_at: datetime
    revoked_at: datetime | None
    revoked_by_email: str | None
    revoke_rationale: str | None
    versions: list[CatalogVersionOut]


class CatalogManageListResponse(BaseModel):
    items: list[CatalogManagedTemplateOut]
    can_publish: bool
    can_review: bool


class CatalogGrantOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    permission: Literal["publish", "review"]
    granted_by_email: str
    created_at: datetime
    revoked_at: datetime | None
    revoked_by_email: str | None


class CatalogGrantListResponse(BaseModel):
    items: list[CatalogGrantOut]


class CatalogDraftCreatedOut(BaseModel):
    template_id: uuid.UUID
    version_id: uuid.UUID
    slug: str
    version: int
    status: Literal["draft"]


class CatalogVersionLifecycleOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    version: int
    status: Literal["pending_review", "approved", "rejected"]
    submitted_at: datetime
    reviewed_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None
    review_rationale: str | None


class CatalogRevokedOut(BaseModel):
    id: uuid.UUID
    slug: str
    revoked_at: datetime
    revoked_by_user_id: uuid.UUID
    revoke_rationale: str
