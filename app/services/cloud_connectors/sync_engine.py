"""Synchronize provider deltas into the restricted document ingestion queue."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import InvalidInputError
from app.db.models import (
    ACL_ACTIVE,
    ACL_QUARANTINED,
    GROUP_DIRECTORY,
    SOURCE_FILE_SERVER,
    AccessGroup,
    CloudSourceAcl,
    Document,
    User,
)
from app.rag.bulk_import import import_folder
from app.rag.parsers.registry import SUPPORTED_EXTENSIONS
from app.schemas.cloud_connectors import CloudConnectorSyncResult
from app.services import audit
from app.services.audit import AuditContext
from app.services.cloud_connectors.base import (
    BaseCloudConnector,
    CloudItem,
    CloudPrincipalPermission,
    PermissionReconciliationPage,
)
from app.services.cloud_connectors.google_drive import GoogleDriveConnector
from app.services.cloud_connectors.sharepoint_onedrive import MicrosoftGraphConnector

logger = logging.getLogger(__name__)
_STAGED_KEY = re.compile(r"^[0-9a-f]{64}$")
_PERMISSION_QUARANTINE_REASONS = {
    "source_permissions_pending",
    "source_permissions_unavailable",
    "source_permissions_unmapped",
}


@dataclass(frozen=True)
class PermissionReconciliationResult:
    scanned: int
    reconciled: int
    quarantined: int
    failed: int
    next_cursor: str | None
    complete: bool


async def execute_permission_reconciliation_page(
    *,
    connector: BaseCloudConnector,
    connector_id: UUID,
    cursor: str | None,
    db: AsyncSession,
    page_size: int = 50,
) -> PermissionReconciliationResult:
    """Refresh ACLs for one bounded inventory page without treating absence as deletion."""
    page: PermissionReconciliationPage = await connector.fetch_permission_reconciliation_page(
        cursor=cursor, page_size=min(max(page_size, 1), 50)
    )
    reconciled = 0
    quarantined = 0
    failed = 0

    for item in page.items:
        if not item.id or item.is_deleted or item.is_folder:
            continue
        uri = _source_uri(connector_id, _remote_key(item.id))
        documents = list(
            (
                await db.scalars(
                    select(Document).where(
                        Document.source_system == SOURCE_FILE_SERVER,
                        Document.source_uri == uri,
                    )
                )
            ).all()
        )
        eligible = [
            document
            for document in documents
            if document.acl_status == ACL_ACTIVE
            or document.acl_quarantine_reason in _PERMISSION_QUARANTINE_REASONS
        ]
        if not eligible:
            continue

        # Revoke before the network refresh so an outage cannot preserve stale provider grants.
        await db.execute(delete(CloudSourceAcl).where(CloudSourceAcl.document_id.in_([d.id for d in eligible])))
        for document in eligible:
            document.acl_status = ACL_QUARANTINED
            document.acl_quarantine_reason = "source_permissions_pending"
        await db.commit()

        try:
            permissions = await connector.fetch_item_permissions(item.id)
            mapped = await _resolve_provider_permissions(db, permissions)
        except Exception as exc:
            logger.warning("Cloud permission reconciliation failed error_type=%s", type(exc).__name__)
            for document in eligible:
                document.acl_quarantine_reason = "source_permissions_unavailable"
            await db.commit()
            failed += 1
            continue

        if mapped is None:
            for document in eligible:
                document.acl_quarantine_reason = "source_permissions_unmapped"
            await db.commit()
            quarantined += len(eligible)
            continue

        for document in eligible:
            for principal_type, principal_id in mapped:
                db.add(
                    CloudSourceAcl(
                        document_id=document.id,
                        principal_user_id=principal_id if principal_type == "user" else None,
                        principal_group_id=principal_id if principal_type == "group" else None,
                    )
                )
            document.acl_status = ACL_ACTIVE
            document.acl_quarantine_reason = None
            document.acl_synced_at = datetime.now(UTC)
        await db.commit()
        reconciled += len(eligible)

    return PermissionReconciliationResult(
        scanned=len(page.items),
        reconciled=reconciled,
        quarantined=quarantined,
        failed=failed,
        next_cursor=page.next_cursor,
        complete=page.complete,
    )


def _remote_key(remote_id: str) -> str:
    if not remote_id:
        raise InvalidInputError("A cloud source item has no stable identity.")
    return hashlib.sha256(remote_id.encode("utf-8")).hexdigest()


def _source_uri(connector_id: UUID, key: str) -> str:
    return f"cloud://{connector_id}/{key}"


def _remove_staged_item(staging_base: Path, key: str, *, except_path: Path | None = None) -> None:
    """Remove only regular files named for the hashed provider item, never follow links."""
    for candidate in staging_base.glob(f"{key}.*"):
        if except_path is not None and candidate == except_path:
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.resolve(strict=True).parent != staging_base.resolve(strict=True):
                continue
            candidate.unlink()
        except OSError:
            # A later retry will revisit the item; cleanup is best effort and fail-closed.
            continue


async def _resolve_provider_permissions(
    db: AsyncSession, permissions: list[CloudPrincipalPermission]
) -> list[tuple[str, UUID]] | None:
    """Resolve every provider ACE to one local identity; unsupported or ambiguous sets fail closed."""
    if not permissions or len(permissions) > 1000:
        return None
    if any(
        permission.access != "allow"
        or permission.principal_type not in {"user", "group"}
        or permission.rights not in {"read", "read_execute", "list", "write", "modify", "full_control"}
        for permission in permissions
    ):
        return None

    user_external_ids = {p.external_id for p in permissions if p.principal_type == "user" and p.external_id}
    user_emails = {p.email.strip().lower() for p in permissions if p.principal_type == "user" and p.email}
    group_external_ids = {p.external_id for p in permissions if p.principal_type == "group" and p.external_id}
    user_rows = (
        await db.execute(
            select(User.id, User.external_id, User.email).where(
                or_(
                    User.external_id.in_(user_external_ids) if user_external_ids else False,
                    User.email.in_(user_emails) if user_emails else False,
                )
            )
        )
    ).all() if user_external_ids or user_emails else []
    group_rows = (
        await db.execute(
            select(AccessGroup.id, AccessGroup.external_id).where(
                AccessGroup.source == GROUP_DIRECTORY,
                AccessGroup.external_id.in_(group_external_ids),
            )
        )
    ).all() if group_external_ids else []

    by_user_key: dict[str, set[UUID]] = {}
    for user_id, external_id, email in user_rows:
        if external_id:
            by_user_key.setdefault(f"id:{external_id}", set()).add(user_id)
        by_user_key.setdefault(f"email:{email.strip().lower()}", set()).add(user_id)
    by_group_key: dict[str, set[UUID]] = {}
    for group_id, external_id in group_rows:
        by_group_key.setdefault(external_id, set()).add(group_id)

    resolved: set[tuple[str, UUID]] = set()
    for permission in permissions:
        candidates: set[UUID] = set()
        if permission.principal_type == "user":
            if permission.external_id:
                candidates.update(by_user_key.get(f"id:{permission.external_id}", set()))
            if permission.email:
                candidates.update(by_user_key.get(f"email:{permission.email.strip().lower()}", set()))
            if len(candidates) != 1:
                return None
            resolved.add(("user", next(iter(candidates))))
        else:
            candidates.update(by_group_key.get(permission.external_id or "", set()))
            if len(candidates) != 1:
                return None
            resolved.add(("group", next(iter(candidates))))
    return sorted(resolved, key=lambda row: (row[0], str(row[1]))) or None


def build_connector_instance(
    connector_type: str,
    credentials: dict[str, Any],
    custom_client=None,
) -> BaseCloudConnector:
    """Instantiate the appropriate connector adapter."""
    if connector_type == "google_drive":
        return GoogleDriveConnector(
            client_id=credentials.get("client_id", ""),
            client_secret=credentials.get("client_secret", ""),
            refresh_token=credentials.get("refresh_token", ""),
            folder_id=credentials.get("folder_id"),
            drive_id=credentials.get("drive_id"),
            client=custom_client,
        )
    elif connector_type in {"onedrive", "sharepoint"}:
        return MicrosoftGraphConnector(
            tenant_id=credentials.get("tenant_id", ""),
            client_id=credentials.get("client_id", ""),
            client_secret=credentials.get("client_secret", ""),
            drive_id=credentials.get("drive_id"),
            site_id=credentials.get("site_id"),
            client=custom_client,
        )
    else:
        raise InvalidInputError(f"Unsupported connector type '{connector_type}'.")


async def execute_connector_sync(
    *,
    connector: BaseCloudConnector,
    connector_id: UUID,
    connector_name: str,
    visibility: str,
    department_group_id: UUID | None,
    cursor: str | None,
    settings: Settings,
    db: AsyncSession,
    audit_ctx: AuditContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin_email: str,
    target_dir: Path | None = None,
) -> CloudConnectorSyncResult:
    """Fetch provider changes, import them as department-restricted documents, and revoke deletions."""
    if visibility != "restricted" or department_group_id is None:
        raise InvalidInputError("Cloud connector documents require an assigned department scope.")

    staging_base = target_dir or (settings.rag_data_dir / "cloud_staging" / str(connector_id) / "ingest")
    staging_base.mkdir(parents=True, exist_ok=True)
    if staging_base.is_symlink():
        raise InvalidInputError("The cloud staging directory is unsafe.")
    staging_base = staging_base.resolve(strict=True)
    delta = await connector.fetch_delta(cursor=cursor)
    discovered = len(delta.items)
    downloaded = 0
    skipped = 0
    deleted_ids = set(delta.deleted_ids)
    deleted_ids.update(item.id for item in delta.items if item.is_deleted and item.id)
    errors: list[str] = []
    changed_names: dict[str, str] = {}
    blocked_keys: set[str] = set()
    provider_acls: dict[str, list[tuple[str, UUID]]] = {}

    async def quarantine_existing(key: str, reason: str) -> None:
        uri = _source_uri(connector_id, key)
        await db.execute(
            delete(CloudSourceAcl).where(
                CloudSourceAcl.document_id.in_(select(Document.id).where(Document.source_uri == uri))
            )
        )
        await db.execute(
            update(Document)
            .where(
                Document.source_system == SOURCE_FILE_SERVER,
                Document.source_uri == uri,
            )
            .values(acl_status=ACL_QUARANTINED, acl_quarantine_reason=reason)
        )
        await db.commit()

    # A provider item ID never enters a path or log; only its fixed-length digest does.
    for item in delta.items:
        if item.is_deleted or item.is_folder:
            continue

        extension = Path(item.name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            try:
                key = _remote_key(item.id)
                blocked_keys.add(key)
                await quarantine_existing(key, "source_sync_pending")
                _remove_staged_item(staging_base, key)
                skipped += 1
            except Exception as exc:
                await db.rollback()
                logger.warning("Cloud connector scope update failed error_type=%s", type(exc).__name__)
                errors.append("A cloud source change could not be applied.")
            continue

        try:
            key = _remote_key(item.id)
            # Remove the old provider grant while the live permission set is refreshed.
            await quarantine_existing(key, "source_permissions_pending")
            remote_permissions = await connector.fetch_item_permissions(item.id)
            mapped_permissions = await _resolve_provider_permissions(db, remote_permissions)
        except Exception as exc:
            logger.warning("Cloud permission refresh failed error_type=%s", type(exc).__name__)
            try:
                blocked_keys.add(_remote_key(item.id))
                await quarantine_existing(_remote_key(item.id), "source_permissions_unavailable")
                _remove_staged_item(staging_base, _remote_key(item.id))
            except Exception as quarantine_exc:
                await db.rollback()
                logger.warning("Cloud connector scope update failed error_type=%s", type(quarantine_exc).__name__)
            errors.append("A cloud source permission could not be verified.")
            skipped += 1
            continue

        if mapped_permissions is None:
            blocked_keys.add(key)
            _remove_staged_item(staging_base, key)
            errors.append("A cloud source permission could not be mapped safely.")
            skipped += 1
            continue
        provider_acls[key] = mapped_permissions

        try:
            content = await connector.download_content(item.id)
            if len(content) > settings.rag_max_upload_mb * 1024 * 1024:
                blocked_keys.add(key)
                await quarantine_existing(key, "source_sync_pending")
                _remove_staged_item(staging_base, key)
                errors.append("A cloud source file exceeded the configured upload limit.")
                continue

            dest_file = staging_base / f"{key}{extension}"
            if dest_file.is_symlink():
                raise OSError("Unsafe cloud staging target.")
            temporary = staging_base / f".{key}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                temporary.replace(dest_file)
            finally:
                temporary.unlink(missing_ok=True)
            _remove_staged_item(staging_base, key, except_path=dest_file)
            changed_names[key] = item.name
            downloaded += 1
        except Exception as exc:
            # Provider exception strings can contain credentials, URLs or private names.
            logger.warning("Cloud connector download failed error_type=%s", type(exc).__name__)
            try:
                blocked_keys.add(key)
                await quarantine_existing(key, "source_sync_pending")
            except Exception as quarantine_exc:
                await db.rollback()
                logger.warning("Cloud connector scope update failed error_type=%s", type(quarantine_exc).__name__)
            errors.append("A cloud source file could not be downloaded.")

    # Revoke deleted items before removing their staging copies. The original stored object,
    # indexed chunks and any earlier version remain available to authorized retention/export code.
    for remote_id in deleted_ids:
        try:
            key = _remote_key(remote_id)
            await quarantine_existing(key, "source_removed")
            _remove_staged_item(staging_base, key)
        except Exception as exc:
            await db.rollback()
            logger.warning("Cloud connector removal sync failed error_type=%s", type(exc).__name__)
            errors.append("A cloud source deletion could not be applied.")

    # Drain previously downloaded items after an interrupted importer run, even when the
    # current provider delta contains no new content. Unknown legacy staging filenames are ignored.
    stage_files = sorted(
        path
        for path in staging_base.iterdir()
        if not path.is_symlink() and path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    source_uris: dict[str, str] = {}
    display_names: dict[str, str] = {}
    for path in stage_files:
        if not _STAGED_KEY.fullmatch(path.stem):
            continue
        key = path.stem
        if key in blocked_keys or key not in provider_acls:
            continue
        uri = _source_uri(connector_id, key)
        relative = path.name
        source_uris[relative] = uri
        if key in changed_names:
            display_names[relative] = changed_names[key]

    queued = 0
    unchanged = 0
    for path in stage_files:
        if path.name not in source_uris:
            continue
        uri = source_uris[path.name]
        key = path.stem
        if path.name not in display_names:
            existing_name = await db.scalar(select(Document.filename).where(Document.source_uri == uri))
            display_names[path.name] = existing_name or Path(path.name).name
        try:
            stats = await import_folder(
                sessionmaker,
                settings,
                staging_base,
                admin_email=admin_email,
                visibility="restricted",
                department_id=str(department_group_id),
                source_id=f"cloud-{connector_id.hex}",
                source_system=SOURCE_FILE_SERVER,
                source_uri_map={path.name: uri},
                display_name_map={path.name: display_names[path.name]},
            )
            item_queued = int(stats.get("queued", 0))
            item_unchanged = int(stats.get("unchanged", 0))
            if int(stats.get("failed", 0)) or int(stats.get("oversized", 0)) or not (
                item_queued or item_unchanged
            ):
                errors.append("A cloud source file could not be imported safely.")
                continue

            # The document remains quarantined until its provider allow-list and local scope are both stored.
            document = await db.scalar(select(Document).where(Document.source_uri == uri).with_for_update())
            if document is None:
                await db.rollback()
                errors.append("A cloud source file could not be authorized safely.")
                continue
            await db.execute(delete(CloudSourceAcl).where(CloudSourceAcl.document_id == document.id))
            for principal_type, principal_id in provider_acls[key]:
                db.add(
                    CloudSourceAcl(
                        document_id=document.id,
                        principal_user_id=principal_id if principal_type == "user" else None,
                        principal_group_id=principal_id if principal_type == "group" else None,
                    )
                )
            document.acl_status = ACL_ACTIVE
            document.acl_quarantine_reason = None
            document.acl_synced_at = datetime.now(UTC)
            await db.commit()
            queued += item_queued
            unchanged += item_unchanged
            try:
                path.unlink()
            except OSError:
                logger.warning("Cloud staging cleanup failed error_type=OSError")
        except Exception as exc:
            logger.warning("Cloud source import failed error_type=%s", type(exc).__name__)
            errors.append("Cloud source files could not be added to the ingestion queue.")

    audit.record(
        db,
        audit_ctx,
        "cloud_connector.sync.completed",
        target=str(connector_id),
        details={
            "files_discovered": discovered,
            "files_synced": downloaded,
            "files_queued": queued,
            "files_unchanged": unchanged,
            "files_skipped": skipped,
            "files_deleted": len(deleted_ids),
            "error_count": len(errors),
        },
    )

    status = "success" if not errors else ("partial" if downloaded or queued or unchanged else "failed")
    return CloudConnectorSyncResult(
        connector_id=connector_id,
        status=status,
        files_discovered=discovered,
        files_synced=downloaded,
        files_queued=queued,
        files_unchanged=unchanged,
        files_skipped=skipped,
        files_deleted=len(deleted_ids),
        errors=errors,
        next_cursor=delta.next_cursor,
    )
