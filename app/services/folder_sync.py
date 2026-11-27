"""Owned, bounded and resumable scheduled folder synchronization."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_ADMIN,
    AccessGroup,
    FolderSyncNotification,
    FolderSyncPreference,
    FolderSyncRun,
    FolderSyncSource,
    User,
)
from app.rag.bulk_import import import_folder
from app.services import audit
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)
_LEASE = timedelta(minutes=5)
_HEARTBEAT = 60
_MAX_RETRY_SECONDS = 3600


def _source_path(settings: Settings, relative_path: str) -> Path:
    """Resolve an import path beneath the operator-managed root; never accept arbitrary host paths."""
    try:
        root = settings.rag_import_root.resolve(strict=True)
    except OSError:
        raise InvalidInputError("The configured folder import root is unavailable.") from None
    if not root.is_dir():
        raise InvalidInputError("The configured folder import root is not a directory.")
    windows_path = PureWindowsPath(relative_path)
    candidate_parts = relative_path.replace("\\", "/").split("/")
    if windows_path.is_absolute() or windows_path.drive or any(part in ("", ".", "..") for part in candidate_parts):
        raise InvalidInputError("The folder path must remain beneath the configured import root.")
    try:
        candidate = (root / Path(*candidate_parts)).resolve(strict=True)
        if candidate == root or not candidate.is_relative_to(root) or not candidate.is_dir():
            raise InvalidInputError("Choose a folder below the configured import root.")
    except OSError:
        raise InvalidInputError("The configured source folder is unavailable.") from None
    return candidate


async def create_source(
    db: AsyncSession,
    actor: User,
    settings: Settings,
    *,
    name: str,
    relative_path: str,
    visibility: str,
    department_group_id: uuid.UUID | None,
    interval_minutes: int,
    ctx: AuditContext,
) -> FolderSyncSource:
    if actor.role != ROLE_ADMIN or not actor.is_active:
        raise PermissionDeniedError()
    _source_path(settings, relative_path)
    if (visibility == "restricted") != (department_group_id is not None):
        raise InvalidInputError("Restricted imports require a department; other scopes do not.")
    if department_group_id is not None:
        group = await db.get(AccessGroup, department_group_id)
        if group is None or group.kind != "department" or group.source != "local":
            raise InvalidInputError("Select a locally managed department.")
    source = FolderSyncSource(
        source_id=uuid.uuid4().hex[:24],
        name=name,
        relative_path=relative_path,
        owner_user_id=actor.id,
        visibility=visibility,
        department_group_id=department_group_id,
        interval_minutes=interval_minutes,
    )
    db.add(source)
    await db.flush()
    audit.record(
        db,
        ctx,
        "folder_sync.source_created",
        target=str(source.id),
        details={"visibility": visibility, "interval_minutes": interval_minutes},
    )
    await db.commit()
    await db.refresh(source)
    return source


async def list_sources(db: AsyncSession) -> list[FolderSyncSource]:
    return list((await db.scalars(select(FolderSyncSource).order_by(FolderSyncSource.created_at.desc()))).all())


async def update_source(
    db: AsyncSession,
    source_id: uuid.UUID,
    *,
    interval_minutes: int | None,
    enabled: bool | None,
    ctx: AuditContext,
) -> FolderSyncSource:
    source = await db.scalar(select(FolderSyncSource).where(FolderSyncSource.id == source_id).with_for_update())
    if source is None:
        raise NotFoundError("Folder sync source not found.")
    changes: dict[str, object] = {}
    if interval_minutes is not None and interval_minutes != source.interval_minutes:
        source.interval_minutes = interval_minutes
        source.next_sync_at = datetime.now(UTC) + timedelta(minutes=interval_minutes)
        changes["interval_minutes"] = interval_minutes
    if enabled is not None and enabled != source.enabled:
        source.enabled = enabled
        if enabled:
            source.next_sync_at = datetime.now(UTC)
        changes["enabled"] = enabled
    if changes:
        audit.record(db, ctx, "folder_sync.source_updated", target=str(source.id), details=changes)
        await db.commit()
        await db.refresh(source)
    return source


async def list_runs(db: AsyncSession, source_id: uuid.UUID) -> list[FolderSyncRun]:
    if await db.get(FolderSyncSource, source_id) is None:
        raise NotFoundError("Folder sync source not found.")
    return list(
        (
            await db.scalars(
                select(FolderSyncRun)
                .where(FolderSyncRun.source_id == source_id)
                .order_by(FolderSyncRun.started_at.desc())
                .limit(100)
            )
        ).all()
    )


async def get_preference(db: AsyncSession, user_id: uuid.UUID) -> bool:
    preference = await db.get(FolderSyncPreference, user_id)
    return preference.change_alerts_enabled if preference else True


async def set_preference(db: AsyncSession, user_id: uuid.UUID, enabled: bool) -> bool:
    await db.execute(
        insert(FolderSyncPreference)
        .values(user_id=user_id, change_alerts_enabled=enabled)
        .on_conflict_do_update(
            index_elements=[FolderSyncPreference.user_id],
            set_={"change_alerts_enabled": enabled, "updated_at": datetime.now(UTC)},
        )
    )
    await db.commit()
    return enabled


async def list_notifications(db: AsyncSession, user_id: uuid.UUID) -> list[tuple[FolderSyncNotification, FolderSyncRun, FolderSyncSource]]:
    rows = await db.execute(
        select(FolderSyncNotification, FolderSyncRun, FolderSyncSource)
        .join(FolderSyncRun, FolderSyncRun.id == FolderSyncNotification.run_id)
        .join(FolderSyncSource, FolderSyncSource.id == FolderSyncRun.source_id)
        .where(FolderSyncNotification.recipient_user_id == user_id)
        .order_by(FolderSyncNotification.created_at.desc())
        .limit(100)
    )
    return list(rows.all())


async def mark_notification_read(db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID) -> FolderSyncNotification:
    notification = await db.scalar(
        select(FolderSyncNotification)
        .where(
            FolderSyncNotification.id == notification_id,
            FolderSyncNotification.recipient_user_id == user_id,
        )
        .with_for_update()
    )
    if notification is None:
        raise NotFoundError("Folder sync notification not found.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(notification)
    return notification


async def _claim_source(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> tuple[uuid.UUID, uuid.UUID] | None:
    async with sessionmaker() as db:
        source = await db.scalar(
            select(FolderSyncSource)
            .where(
                FolderSyncSource.enabled.is_(True),
                or_(
                    FolderSyncSource.next_sync_at <= now,
                    FolderSyncSource.locked_until <= now,
                ),
                or_(FolderSyncSource.locked_until.is_(None), FolderSyncSource.locked_until <= now),
            )
            .order_by(FolderSyncSource.next_sync_at, FolderSyncSource.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if source is None:
            return None
        if source.locked_until is not None and source.locked_until <= now:
            stale = await db.scalar(
                select(FolderSyncRun)
                .where(FolderSyncRun.source_id == source.id, FolderSyncRun.status == "running")
                .order_by(FolderSyncRun.started_at.desc())
                .limit(1)
                .with_for_update()
            )
            if stale is not None:
                stale.status = "failed"
                stale.error_code = "lease_expired"
                stale.finished_at = now
                source.consecutive_failures = min(source.consecutive_failures + 1, 12)
                source.last_error_code = "lease_expired"
                preference = await db.get(FolderSyncPreference, source.owner_user_id)
                if preference is None or preference.change_alerts_enabled:
                    await db.execute(
                        insert(FolderSyncNotification)
                        .values(run_id=stale.id, recipient_user_id=source.owner_user_id)
                        .on_conflict_do_nothing(constraint="uq_folder_sync_notification_recipient")
                    )
                audit.record(
                    db,
                    AuditContext(None, "folder-sync-worker", None, None),
                    "folder_sync.run_finished",
                    target=source.source_id,
                    outcome="failure",
                    details={"status": "failed", "error_code": "lease_expired", "attempt": stale.attempt},
                )
        source.locked_until = now + _LEASE
        source.next_sync_at = now + timedelta(minutes=source.interval_minutes)
        run = FolderSyncRun(source_id=source.id, attempt=source.consecutive_failures + 1)
        db.add(run)
        await db.flush()
        source.locked_run_id = run.id
        result = (source.id, run.id)
        await db.commit()
        return result


async def _renew_lease(
    sessionmaker: async_sessionmaker[AsyncSession], source_id: uuid.UUID, run_id: uuid.UUID, stopped: asyncio.Event
) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=_HEARTBEAT)
            return
        except TimeoutError:
            pass
        try:
            async with sessionmaker() as db:
                await db.execute(
                    FolderSyncSource.__table__.update()
                    .where(FolderSyncSource.id == source_id, FolderSyncSource.locked_run_id == run_id)
                    .values(locked_until=datetime.now(UTC) + _LEASE)
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Folder sync lease renewal failed (%s)", type(exc).__name__)


async def _finish_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    stats: dict[str, int | str],
    *,
    error_code: str | None,
    now: datetime,
) -> None:
    async with sessionmaker() as db:
        source = await db.scalar(select(FolderSyncSource).where(FolderSyncSource.id == source_id).with_for_update())
        run = await db.scalar(select(FolderSyncRun).where(FolderSyncRun.id == run_id).with_for_update())
        if source is None or run is None:
            return
        if source.locked_run_id != run_id:
            return  # a newer lease took ownership after this worker stalled
        for column in ("scanned", "created", "updated", "unchanged", "removed", "duplicates", "failed"):
            setattr(run, column, int(stats.get(column, 0)))
        run.error_code = error_code or ("file_failures" if int(stats.get("failed", 0)) else None)
        run.status = "failed" if error_code else "partial" if int(stats.get("failed", 0)) else "completed"
        run.finished_at = now
        source.locked_until = None
        source.locked_run_id = None
        source.last_run_at = now
        if run.status in ("failed", "partial"):
            if run.error_code == "owner_inactive":
                source.enabled = False
            source.consecutive_failures = min(source.consecutive_failures + 1, 12)
            source.last_error_code = run.error_code
            delay = min(_MAX_RETRY_SECONDS, 30 * (2 ** min(source.consecutive_failures - 1, 7)))
            source.next_sync_at = now + timedelta(seconds=delay)
        else:
            source.consecutive_failures = 0
            source.last_error_code = None
            source.next_sync_at = now + timedelta(minutes=source.interval_minutes)

        preference = await db.get(FolderSyncPreference, source.owner_user_id)
        has_changes = any(int(stats.get(key, 0)) for key in ("created", "updated", "removed"))
        if (has_changes or run.status in ("failed", "partial")) and (preference is None or preference.change_alerts_enabled):
            await db.execute(
                insert(FolderSyncNotification)
                .values(run_id=run.id, recipient_user_id=source.owner_user_id)
                .on_conflict_do_nothing(constraint="uq_folder_sync_notification_recipient")
            )
        audit.record(
            db,
            AuditContext(None, "folder-sync-worker", None, None),
            "folder_sync.run_finished",
            target=source.source_id,
            outcome="failure" if run.status in ("failed", "partial") else "success",
            details={
                "status": run.status,
                "scanned": run.scanned,
                "created": run.created,
                "updated": run.updated,
                "removed": run.removed,
                "failed": run.failed,
            },
        )
        await db.commit()


async def process_folder_sync_once(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime | None = None
) -> bool:
    """Claim one due source, run the resumable importer, and record an idempotent count-only diff."""
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The folder sync scheduler requires a timezone-aware timestamp.")
    claimed = await _claim_source(sessionmaker, current)
    if claimed is None:
        return False
    source_id, run_id = claimed
    stopped = asyncio.Event()
    heartbeat = asyncio.create_task(_renew_lease(sessionmaker, source_id, run_id, stopped))
    stats: dict[str, int | str] = {}
    error_code: str | None = None
    try:
        import_request: tuple[str, str, str, str | None, str] | None = None
        async with sessionmaker() as db:
            source = await db.get(FolderSyncSource, source_id)
            owner = await db.get(User, source.owner_user_id) if source else None
            if source is None or owner is None or not owner.is_active or owner.role != ROLE_ADMIN:
                error_code = "owner_inactive"
            else:
                import_request = (
                    owner.email,
                    source.visibility,
                    source.source_id,
                    str(source.department_group_id) if source.department_group_id else None,
                    source.relative_path,
                )
        if import_request is not None:
            admin_email, visibility, source_key, department_id, relative_path = import_request
            folder = _source_path(settings, relative_path)
            stats = await import_folder(
                sessionmaker,
                settings,
                folder,
                admin_email=admin_email,
                visibility=visibility,
                department_id=department_id,
                source_id=source_key,
                run_id=run_id,
                detect_removed=True,
            )
    except Exception as exc:
        # Error strings can contain local paths; only stable exception types are persisted or logged.
        error_code = type(exc).__name__[:64]
        logger.warning("Scheduled folder sync failed (%s)", error_code)
    finally:
        stopped.set()
        await heartbeat
    await _finish_run(sessionmaker, source_id, run_id, stats, error_code=error_code, now=datetime.now(UTC))
    return True


async def process_due_folder_syncs(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, *, batch_size: int = 10
) -> int:
    processed = 0
    while processed < batch_size and await process_folder_sync_once(sessionmaker, settings):
        processed += 1
    return processed


async def run_folder_sync_worker(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, stop: asyncio.Event, *, interval_seconds: float = 10
) -> None:
    while not stop.is_set():
        try:
            worked = await process_folder_sync_once(sessionmaker, settings)
        except Exception as exc:
            logger.warning("Folder sync worker iteration failed (%s)", type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
