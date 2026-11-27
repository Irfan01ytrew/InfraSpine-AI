import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.permission_catalog import KNOWLEDGE_CATEGORIES, PERMISSIONS

GroupKind = Literal["department", "project", "client", "custom"]
LifecycleStatus = Literal["active", "archived"]
Permission = Literal[*PERMISSIONS]
Category = Literal[*KNOWLEDGE_CATEGORIES]
ScopeDimension = Literal["category", "access_group"]


class PermissionCatalogEntry(BaseModel):
    permission: Permission
    resource: str
    action: str
    scope_dimensions: list[ScopeDimension]


class PermissionCatalogResponse(BaseModel):
    permissions: list[PermissionCatalogEntry]


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: GroupKind
    source: Literal["local", "directory"]
    external_id: str | None
    member_count: int
    created_at: datetime
    parent_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    lifecycle_status: LifecycleStatus = "active"


class GroupListResponse(BaseModel):
    groups: list[GroupOut]


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    kind: GroupKind
    # Set only to adopt a company-directory group; its membership then comes from the directory.
    external_id: str | None = Field(default=None, min_length=1, max_length=256)
    parent_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None


class UpdateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    lifecycle_status: LifecycleStatus | None = None


class WorkspaceSummaryReport(BaseModel):
    group_id: uuid.UUID
    name: str
    kind: str
    parent_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    lifecycle_status: str
    member_count: int
    child_project_count: int
    total_documents: int
    total_bytes: int
    total_chunks: int


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str


class MemberListResponse(BaseModel):
    members: list[MemberOut]


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=320)


class GrantOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    permission: Permission
    category: Category | None
    scope_group_id: uuid.UUID | None
    granted_by: uuid.UUID
    granted_at: datetime
    revoked_at: datetime | None


class GrantListResponse(BaseModel):
    grants: list[GrantOut]


class CreateGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=320)
    permission: Permission
    category: Category | None = None  # None = every category
    scope_group_id: uuid.UUID | None = None  # None = organization-wide


class QuarantinedDocumentOut(BaseModel):
    """Identifiers and reason codes only: names, paths and content of quarantined documents are withheld."""

    id: uuid.UUID
    source_system: str
    reason: str
    acl_synced_at: datetime | None
    created_at: datetime


class QuarantineResponse(BaseModel):
    documents: list[QuarantinedDocumentOut]


class DocumentAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    visibility: Literal["private", "shared", "restricted"]
    user_emails: list[str] = Field(default_factory=list, max_length=200)
    group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
