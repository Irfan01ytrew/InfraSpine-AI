"""Admin provider-management contracts. No response model contains a secret field."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelOptionOut(BaseModel):
    id: str
    label: str
    description: str
    context_window: int
    max_output_tokens: int
    default_output_tokens: int


class SelectOptionOut(BaseModel):
    value: str
    label: str


class SettingFieldOut(BaseModel):
    key: str
    label: str
    type: Literal["number", "integer", "select"]
    default: float | str
    description: str
    minimum: float | None
    maximum: float | None
    step: float | None
    options: list[SelectOptionOut]
    per_model_limit: bool


class ProviderMetadata(BaseModel):
    id: str
    display_name: str
    description: str
    docs_url: str
    models: list[ModelOptionOut]
    default_model: str
    settings: list[SettingFieldOut]


class ProviderCatalogResponse(BaseModel):
    providers: list[ProviderMetadata]


class CreateCustomProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=80)
    base_url: str = Field(..., min_length=8, max_length=500)
    model: str = Field(..., min_length=1, max_length=100)
    api_key: SecretStr = Field(..., min_length=16, max_length=512)


class CredentialSummary(BaseModel):
    id: uuid.UUID
    key_hint: str = Field(description="Last 4 characters of the key, for identification only.")
    verified_at: datetime
    activated_at: datetime | None
    created_at: datetime


class ProviderState(BaseModel):
    provider: str
    display_name: str
    base_url: str | None = None
    configured: bool
    model: str
    settings: dict[str, int | float | str]
    is_active: bool
    version: int = Field(description="Send back as expected_version on the next change.")
    active_credential: CredentialSummary | None
    pending_credential: CredentialSummary | None
    updated_at: datetime | None
    # An admin authorized sending pictures from documents to this provider (see ImageSharingRequest).
    image_sharing_authorized: bool = False
    image_sharing_authorized_at: datetime | None = None


class ProviderListResponse(BaseModel):
    mode: Literal["database"] = "database"
    providers: list[ProviderState]


class UpdateProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., min_length=1, max_length=100)
    settings: dict[str, int | float | str] = Field(default_factory=dict)
    base_url: str | None = Field(default=None, max_length=500)
    # Required when (and only accepted when) a custom provider's base_url changes: stored keys are
    # bound to the endpoint they were verified against and are never sent to a new one.
    api_key: SecretStr | None = Field(default=None, min_length=16, max_length=512)
    expected_version: int = Field(..., ge=0)


class StageCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(..., min_length=16, max_length=512)
    expected_version: int = Field(..., ge=1)


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, min_length=16, max_length=512)
    model: str | None = Field(default=None, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)


class TestConnectionResponse(BaseModel):
    ok: bool
    reason: str | None
    message: str
    detail: str | None = None
    tested: Literal["provided_key", "pending_key", "active_key"]
    model: str
    latency_ms: int


class VersionedActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)


class ImageSharingRequest(BaseModel):
    """Authorize (or withdraw authorization) to send pictures from documents to this provider for description."""

    model_config = ConfigDict(extra="forbid")

    authorized: bool
    expected_version: int = Field(..., ge=1)


class AuditLogEntry(BaseModel):
    id: int
    created_at: datetime
    actor_email: str | None
    action: str
    target: str | None
    outcome: str
    details: dict
    ip_address: str | None


class AuditLogResponse(BaseModel):
    entries: list[AuditLogEntry]
