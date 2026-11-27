"""Schemas for isolated customer/partner identities and workspaces."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.services.auth import normalize_email


class PartnerWorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
    name: str = Field(min_length=2, max_length=160)


class PartnerWorkspaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    created_at: datetime


class PartnerWorkspaceStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern=r"^(active|suspended)$")


class PartnerInviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_and_validate_email(cls, value: str) -> str:
        email = normalize_email(value)
        if email.count("@") != 1 or "." not in email.rsplit("@", 1)[-1] or any(ch.isspace() for ch in email):
            raise ValueError("Enter a valid email address.")
        return email


class PartnerInvitationOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    token: str | None = None


class PartnerInvitationListResponse(BaseModel):
    invitations: list[PartnerInvitationOut]


class PartnerIdentityAdminOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime


class PartnerIdentityActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool


class PartnerInviteAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(min_length=20, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=1024)


class PartnerLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    workspace_slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class PartnerIdentityOut(BaseModel):
    id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    workspace_slug: str
    workspace_name: str


class PartnerKnowledgeSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=50_000)


class PartnerKnowledgeSourceOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    content_sha256: str
    status: str
    created_at: datetime
    archived_at: datetime | None
    source_format: str = "text"
    index_status: str = "pending"
    index_attempts: int = 0
    index_error_code: str | None = None


class PartnerAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=4000)


class PartnerCitationOut(BaseModel):
    source_id: str
    source_record_id: uuid.UUID
    title: str
    excerpt: str = Field(max_length=320)
    location_label: str | None = None
    download_available: bool = False


class PartnerAskResponse(BaseModel):
    answer: str
    insufficient_evidence: bool
    citations: list[PartnerCitationOut] = Field(max_length=8)
    retrieved_sources: int
