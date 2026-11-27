"""Request and response types for secure scheduled folder synchronization."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class FolderSyncSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(min_length=1, max_length=1024)
    visibility: Literal["private", "shared", "restricted"] = "restricted"
    department_group_id: UUID | None = None
    interval_minutes: int = Field(default=60, ge=5, le=10080)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("A source name is required.")
        return value

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        value = value.strip().replace("\\", "/")
        parts = value.split("/")
        if value.startswith("/") or ":" in value or any(part in ("", ".", "..") for part in parts):
            raise ValueError("Choose a non-empty relative folder path without dot segments.")
        return "/".join(parts)

    @model_validator(mode="after")
    def department_scope(self):
        if (self.visibility == "restricted") != (self.department_group_id is not None):
            raise ValueError("Restricted imports require a department; other scopes do not.")
        return self


class FolderSyncSourceUpdate(BaseModel):
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    enabled: bool | None = None


class FolderSyncSourceOut(BaseModel):
    id: UUID
    source_id: str
    name: str
    relative_path: str
    owner_user_id: UUID
    visibility: Literal["private", "shared", "restricted"]
    department_group_id: UUID | None
    interval_minutes: int
    enabled: bool
    next_sync_at: datetime
    last_run_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None

    model_config = {"from_attributes": True}


class FolderSyncRunOut(BaseModel):
    id: UUID
    status: Literal["running", "completed", "partial", "failed"]
    attempt: int
    scanned: int
    created: int
    updated: int
    unchanged: int
    removed: int
    duplicates: int
    failed: int
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class FolderSyncRunList(BaseModel):
    items: list[FolderSyncRunOut]


class FolderSyncSourceList(BaseModel):
    items: list[FolderSyncSourceOut]


class FolderSyncNotificationOut(BaseModel):
    id: UUID
    source_id: UUID
    source_name: str
    run_id: UUID
    status: Literal["completed", "partial", "failed"]
    created: int
    updated: int
    removed: int
    failed: int
    error_code: str | None
    created_at: datetime
    read_at: datetime | None


class FolderSyncNotificationList(BaseModel):
    items: list[FolderSyncNotificationOut]


class FolderSyncPreferenceIn(BaseModel):
    change_alerts_enabled: bool


class FolderSyncPreferenceOut(BaseModel):
    change_alerts_enabled: bool
