"""Resumable local folder import. Re-running the same source ID skips unchanged files and versions changes."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from collections.abc import Mapping

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.errors import InvalidInputError
from app.db.models import (
    ROLE_ADMIN,
    VISIBILITY_PRIVATE,
    VISIBILITY_RESTRICTED,
    VISIBILITY_SHARED,
    AccessGroup,
    ACL_ACTIVE,
    ACL_QUARANTINED,
    SOURCE_FILE_SERVER,
    SOURCE_UPLOAD,
    Document,
    DocumentVersion,
    ResourceAcl,
    User,
)
from app.rag.parsers.base import ParseError
from app.rag.file_limits import max_ingested_bytes
from app.rag.parsers.registry import SUPPORTED_EXTENSIONS, format_for_filename, inspect_document, limits_from_settings
from app.rag.storage import DocumentStorage, UnsafeFileError, sanitize_filename
from app.services.auth import normalize_email

_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def default_source_id(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()[:24]


def _files(root: Path):
    """Walk without following directory or file symlinks and keep memory use independent of file count."""
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(name for name in directories if not _is_link_or_junction(current_path / name))
        for name in sorted(names):
            path = current_path / name
            if _is_link_or_junction(path) or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                if not path.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            yield path


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _is_reparse_point(info) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _same_file(left, right) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _open_import_source(root: Path, path: Path):
    """Open a pinned regular file beneath root and refuse symlink/junction swaps around open()."""
    root_resolved = root.resolve(strict=True)
    source_resolved = path.resolve(strict=True)
    if not source_resolved.is_relative_to(root_resolved) or _is_link_or_junction(path):
        raise UnsafeFileError("unsafe_import_path", "The import source changed or escaped its configured root.")
    before = path.stat(follow_symlinks=False)
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise UnsafeFileError("unsafe_import_path", "Only regular files beneath the configured root can be imported.")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = path.stat(follow_symlinks=False)
        resolved_after = path.resolve(strict=True)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(after)
            or not _same_file(before, opened)
            or not _same_file(opened, after)
            or not resolved_after.is_relative_to(root_resolved)
            or _is_link_or_junction(path)
        ):
            raise UnsafeFileError("unsafe_import_path", "The import source changed or escaped its configured root.")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _sha256(source) -> str:
    digest = hashlib.sha256()
    source.seek(0)
    while block := source.read(1024 * 1024):
        digest.update(block)
    source.seek(0)
    return digest.hexdigest()


def _save(storage: DocumentStorage, source, max_bytes: int, extension: str):
    source.seek(0)
    return storage.save(source, max_bytes, extension)


def _lock_key(source_uri: str) -> int:
    return int.from_bytes(hashlib.sha256(source_uri.encode("utf-8")).digest()[:8], "big", signed=True)


async def import_folder(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    root: Path,
    *,
    admin_email: str,
    visibility: str,
    department_id: str | None,
    source_id: str | None = None,
    retry_failed: bool = True,
    run_id: uuid.UUID | None = None,
    detect_removed: bool = False,
    source_system: str = SOURCE_UPLOAD,
    source_uri_map: Mapping[str, str] | None = None,
    display_name_map: Mapping[str, str] | None = None,
) -> dict[str, int | str]:
    """Import or synchronize files. Per-file commits make interruption safe; an unchanged source URI/hash is a no-op."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise InvalidInputError("The import source must be an existing folder.")
    source_id = source_id or default_source_id(root)
    if not _SOURCE_ID.fullmatch(source_id):
        raise InvalidInputError("Source ID may contain only letters, numbers, dots, underscores and hyphens.")
    if detect_removed and run_id is None:
        raise InvalidInputError("Removal detection requires a persisted folder sync run ID.")
    if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED, VISIBILITY_RESTRICTED):
        raise InvalidInputError("Unsupported document visibility.")
    if source_system not in (SOURCE_UPLOAD, SOURCE_FILE_SERVER):
        raise InvalidInputError("Unsupported document source system.")
    if source_system == SOURCE_FILE_SERVER and (visibility != VISIBILITY_RESTRICTED or department_id is None):
        raise InvalidInputError("Synced documents require a restricted department scope.")
    if source_system == SOURCE_FILE_SERVER and source_uri_map is None:
        raise InvalidInputError("Synced document imports require explicit stable source identities.")
    if (visibility == VISIBILITY_RESTRICTED) != (department_id is not None):
        raise InvalidInputError("Restricted folder imports require a department ID; other scopes do not.")

    async with sessionmaker() as db:
        admin = (
            await db.execute(select(User).where(User.email == normalize_email(admin_email)))
        ).scalar_one_or_none()
        if admin is None or admin.role != ROLE_ADMIN:
            raise InvalidInputError("Folder import requires an existing administrator account.")
        department = None
        if department_id:
            try:
                department_uuid = __import__("uuid").UUID(department_id)
            except ValueError:
                raise InvalidInputError("The department ID is invalid.") from None
            department = (
                await db.execute(select(AccessGroup).where(AccessGroup.id == department_uuid))
            ).scalar_one_or_none()
            if department is None or department.kind != "department" or department.source != "local":
                raise InvalidInputError("The selected department is not a locally managed department.")
        await db.commit()
        owner_id = admin.id

    storage = DocumentStorage(settings.rag_data_dir.resolve() / "uploads")
    stats: dict[str, int | str] = {
        "source_id": source_id,
        "scanned": 0,
        "queued": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "duplicates": 0,
        "failed": 0,
        "oversized": 0,
    }
    seen_source_uris: set[str] = set()
    for path in _files(root):
        stats["scanned"] = int(stats["scanned"]) + 1
        relative = path.relative_to(root).as_posix()
        if source_system == SOURCE_FILE_SERVER and relative not in (source_uri_map or {}):
            continue
        source_uri = (source_uri_map or {}).get(relative, f"folder-import://{source_id}/{relative}")
        if not source_uri or len(source_uri) > 1024 or source_uri in seen_source_uris:
            stats["failed"] = int(stats["failed"]) + 1
            continue
        seen_source_uris.add(source_uri)
        source = None
        stored_key = None
        stored_attached = False
        try:
            file_format = format_for_filename(path.name)
            maximum_bytes = max_ingested_bytes(settings, file_format, imported=True)
            source = await asyncio.to_thread(_open_import_source, root, path)
            if os.fstat(source.fileno()).st_size > maximum_bytes:
                stats["oversized"] = int(stats["oversized"]) + 1
                stats["failed"] = int(stats["failed"]) + 1
                continue
            digest = await asyncio.to_thread(_sha256, source)
            async with sessionmaker() as db:
                # Concurrent resumptions for one path serialize without blocking imports of other files.
                await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(source_uri)})
                existing = (
                    await db.execute(select(Document).where(Document.source_uri == source_uri).with_for_update())
                ).scalar_one_or_none()
                if existing is not None and existing.source_system != source_system:
                    await db.rollback()
                    stats["failed"] = int(stats["failed"]) + 1
                    continue
                if existing is not None:
                    current_acl = (
                        await db.execute(
                            select(ResourceAcl.principal_user_id, ResourceAcl.principal_group_id).where(
                                ResourceAcl.document_id == existing.id
                            )
                        )
                    ).all()
                    expected_acl = [(None, department.id)] if department else []
                    if (
                        existing.visibility != visibility
                        or sorted(current_acl, key=lambda row: (str(row[0]), str(row[1])))
                        != sorted(expected_acl, key=lambda row: (str(row[0]), str(row[1])))
                    ):
                        await db.rollback()
                        stats["failed"] = int(stats["failed"]) + 1
                        continue  # never widen/change an existing source document's ACL during content sync
                if existing is not None and existing.sha256 == digest:
                    if display_name_map and relative in display_name_map:
                        existing.filename = sanitize_filename(display_name_map[relative], SUPPORTED_EXTENSIONS)
                    if run_id is not None:
                        existing.folder_sync_run_id = run_id
                    if (
                        not existing.source_uri.startswith("cloud://")
                        and existing.acl_quarantine_reason in {"source_removed", "source_sync_pending"}
                    ):
                        existing.acl_status = ACL_ACTIVE
                        existing.acl_quarantine_reason = None
                        existing.acl_synced_at = func.now()
                    if existing.status == "failed" and retry_failed:
                        existing.status = "pending"
                        existing.attempts = 0
                        existing.next_attempt_at = func.now()
                        existing.error_code = existing.error_message = None
                        await db.commit()
                        stats["queued"] = int(stats["queued"]) + 1
                    else:
                        await db.commit()
                        stats["unchanged"] = int(stats["unchanged"]) + 1
                    continue

                filename = sanitize_filename(
                    (display_name_map or {}).get(relative, path.name), SUPPORTED_EXTENSIONS
                )
                duplicate_id = (
                    await db.execute(
                        select(Document.id).where(Document.sha256 == digest, Document.source_uri != source_uri).limit(1)
                    )
                ).scalar_one_or_none()
                stored = await asyncio.to_thread(
                    _save, storage, source, maximum_bytes, path.suffix.lower()
                )
                stored_key = stored.key
                if stored.sha256 != digest:
                    await db.rollback()
                    stats["failed"] = int(stats["failed"]) + 1
                    continue  # source changed while it was being copied; next sync will retry
                inspection = await asyncio.to_thread(
                    inspect_document, storage.path_for(stored.key), filename, limits_from_settings(settings)
                )

                change_kind = "created" if existing is None else "updated"
                if existing is None:
                    document = Document(
                        owner_id=owner_id,
                        visibility=visibility,
                        source_system=source_system,
                        source_uri=source_uri,
                        folder_sync_run_id=run_id,
                        filename=filename,
                        file_format=inspection.file_format,
                        storage_key=stored.key,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        page_count=inspection.unit_count,
                        status="pending",
                    )
                    db.add(document)
                    await db.flush()
                    if department is not None:
                        db.add(
                            ResourceAcl(
                                resource_type="document",
                                document_id=document.id,
                                principal_group_id=department.id,
                                origin="manual",
                                created_by=owner_id,
                            )
                        )
                        await db.flush()
                    if source_system == SOURCE_FILE_SERVER:
                        if source_uri.startswith("cloud://"):
                            # The provider allow-list is committed separately before retrieval is enabled.
                            document.acl_status = ACL_QUARANTINED
                            document.acl_quarantine_reason = "source_permissions_pending"
                        else:
                            document.acl_status = ACL_ACTIVE
                            document.acl_quarantine_reason = None
                            document.acl_synced_at = func.now()
                else:
                    db.add(
                        DocumentVersion(
                            document_id=existing.id,
                            version=existing.version,
                            filename=existing.filename,
                            file_format=existing.file_format,
                            storage_key=existing.storage_key,
                            sha256=existing.sha256,
                            size_bytes=existing.size_bytes,
                            archived_by=owner_id,
                            reason="folder_sync",
                        )
                    )
                    existing.filename = filename
                    existing.file_format = inspection.file_format
                    existing.storage_key = stored.key
                    existing.sha256 = stored.sha256
                    existing.size_bytes = stored.size_bytes
                    existing.page_count = inspection.unit_count
                    existing.folder_sync_run_id = run_id
                    if display_name_map and relative in display_name_map:
                        existing.filename = filename
                    if (
                        not existing.source_uri.startswith("cloud://")
                        and existing.acl_quarantine_reason in {"source_removed", "source_sync_pending"}
                    ):
                        existing.acl_status = ACL_ACTIVE
                        existing.acl_quarantine_reason = None
                        existing.acl_synced_at = func.now()
                    existing.version += 1
                    existing.status = "pending"
                    existing.attempts = 0
                    existing.next_attempt_at = func.now()
                    existing.locked_until = None
                    existing.error_code = existing.error_message = None
                await db.commit()
                stored_attached = True
                stats["queued"] = int(stats["queued"]) + 1
                stats[change_kind] = int(stats[change_kind]) + 1
                if duplicate_id is not None:
                    # Keep separate rows and ACLs; deduplicating across scopes could broaden access.
                    stats["duplicates"] = int(stats["duplicates"]) + 1
        except (ParseError, UnsafeFileError, OSError, ValueError):
            stats["failed"] = int(stats["failed"]) + 1
            continue
        except Exception:
            stats["failed"] = int(stats["failed"]) + 1
            continue
        finally:
            if source is not None:
                source.close()
            if stored_key is not None and not stored_attached:
                await asyncio.to_thread(storage.delete, stored_key)

    # Do not infer deletions from an incomplete scan: unreadable/oversized files or a parser/storage
    # failure can make an existing source path disappear from this pass. Files remain on disk and in
    # PostgreSQL; a complete scan only quarantines them from retrieval until they reappear.
    if detect_removed and int(stats["failed"]) == 0 and int(stats["oversized"]) == 0:
        prefix = f"folder-import://{source_id}/"
        async with sessionmaker() as db:
            result = await db.execute(
                update(Document)
                .where(
                    Document.source_uri.is_not(None),
                    func.left(Document.source_uri, len(prefix)) == prefix,
                    Document.folder_sync_run_id.is_distinct_from(run_id),
                    Document.acl_status == "active",
                )
                .values(acl_status="quarantined", acl_quarantine_reason="source_removed")
            )
            await db.commit()
            stats["removed"] = max(0, result.rowcount or 0)
    return stats
