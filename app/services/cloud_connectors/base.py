"""Base abstractions for enterprise cloud connectors."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class CloudItem:
    """Represents a file or metadata change from a cloud repository."""
    id: str
    name: str
    mime_type: str
    size_bytes: int = 0
    etag: str | None = None
    remote_path: str = ""
    is_deleted: bool = False
    is_folder: bool = False
    modified_at: datetime | None = None
    remote_acls: list[str] = field(default_factory=list)
    download_url: str | None = None


@dataclass(frozen=True)
class CloudPrincipalPermission:
    """One provider permission, kept separate from the local department boundary."""

    principal_type: str
    external_id: str | None = None
    email: str | None = None
    access: str = "allow"
    rights: str = "read"


@dataclass(frozen=True)
class DeltaSyncResult:
    """Result of a delta synchronization batch."""
    items: list[CloudItem]
    next_cursor: str | None
    has_more: bool = False
    deleted_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionReconciliationPage:
    """One bounded provider inventory page for a non-destructive ACL reconciliation pass."""

    items: list[CloudItem]
    next_cursor: str | None
    complete: bool


class BaseCloudConnector(abc.ABC):
    """Abstract interface for all remote cloud drive and document store connectors."""

    @abc.abstractmethod
    async def validate_connection(self) -> dict[str, Any]:
        """Validate connection credentials and permissions without persisting state."""
        pass

    @abc.abstractmethod
    async def fetch_delta(self, cursor: str | None = None, page_size: int = 100) -> DeltaSyncResult:
        """Fetch changes (creations, modifications, deletions) since the last cursor."""
        pass

    @abc.abstractmethod
    async def download_content(self, item_id: str) -> bytes:
        """Stream/download file content bytes for an authorized item."""
        pass

    async def fetch_item_permissions(self, item_id: str) -> list[CloudPrincipalPermission]:
        """Read the current explicit item permissions; adapters must fail closed on provider errors."""
        raise NotImplementedError("This connector cannot revalidate item permissions.")

    async def fetch_permission_reconciliation_page(
        self, cursor: str | None = None, page_size: int = 50
    ) -> PermissionReconciliationPage:
        """Enumerate one bounded library page without inferring deletion from absence."""
        raise NotImplementedError("This connector cannot enumerate a reconciliation page.")

    @abc.abstractmethod
    def map_acls(self, remote_acls: list[str], fallback_visibility: str = "restricted") -> dict[str, Any]:
        """Map cloud identity roles or email addresses to local authorization principals."""
        pass
