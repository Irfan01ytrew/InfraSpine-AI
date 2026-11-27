"""Pydantic models for Cloud Storage Connectors (Google Drive, OneDrive, SharePoint)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class CloudConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    connector_type: Literal["google_drive", "onedrive", "sharepoint"]
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=512)
    tenant_id: str | None = Field(default=None, max_length=128)  # Required for OneDrive/SharePoint
    refresh_token: str | None = Field(default=None, max_length=2048)  # Required for Google Drive
    drive_id: str | None = Field(default=None, max_length=256)
    folder_id: str | None = Field(default=None, max_length=256)
    # Imported copies are never org-wide or owner-only: the administrator selects
    # the local department that is authorized to use this connector's documents.
    visibility: Literal["restricted"] = "restricted"
    department_group_id: UUID
    sync_interval_minutes: int = Field(default=60, ge=5, le=10080)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Connector name must not be blank.")
        return value

    @model_validator(mode="after")
    def required_provider_fields(self) -> "CloudConnectorCreate":
        if self.connector_type == "google_drive" and not self.refresh_token:
            raise ValueError("Google Drive requires a refresh token.")
        if self.connector_type in {"onedrive", "sharepoint"} and not self.tenant_id:
            raise ValueError("Microsoft connectors require a tenant ID.")
        if not self.department_group_id:
            raise ValueError("Cloud connector imports require a department access group.")
        return self


class CloudConnectorOut(BaseModel):
    id: UUID
    name: str
    connector_type: Literal["google_drive", "onedrive", "sharepoint"]
    visibility: Literal["private", "shared", "restricted"]
    department_group_id: UUID | None
    sync_interval_minutes: int
    is_active: bool
    sync_in_progress: bool = False
    last_sync_status: Literal["success", "partial", "failed"] | None = None
    last_sync_error_count: int = 0
    last_synced_at: datetime | None
    # Provider delta cursors may be bearer-like opaque tokens, so they stay server-side.
    last_cursor: str | None = Field(default=None, exclude=True)
    created_at: datetime
    # Secrets (client_secret, refresh_token) are NEVER included in response representations


class CloudConnectorSyncResult(BaseModel):
    connector_id: UUID
    status: Literal["success", "partial", "failed"]
    files_discovered: int
    files_synced: int
    files_queued: int = 0
    files_unchanged: int = 0
    files_skipped: int = 0
    files_deleted: int
    permission_reconciliation_scanned: int = 0
    permission_reconciliation_updated: int = 0
    permission_reconciliation_quarantined: int = 0
    permission_reconciliation_failed: int = 0
    permission_reconciliation_complete: bool | None = None
    errors: list[str] = Field(default_factory=list)
    # Provider cursors can contain opaque sync tokens and are persisted only in the database.
    next_cursor: str | None = Field(default=None, exclude=True)
