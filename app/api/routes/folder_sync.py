"""Administrator-owned folder synchronization and user-scoped change alerts."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import FolderSyncNotification, FolderSyncRun, FolderSyncSource
from app.schemas.folder_sync import (
    FolderSyncNotificationList,
    FolderSyncNotificationOut,
    FolderSyncPreferenceIn,
    FolderSyncPreferenceOut,
    FolderSyncRunList,
    FolderSyncRunOut,
    FolderSyncSourceCreate,
    FolderSyncSourceList,
    FolderSyncSourceOut,
    FolderSyncSourceUpdate,
)
from app.services import folder_sync

router = APIRouter(prefix="/api/folder-sync", tags=["folder sync"], dependencies=[Depends(get_current_user)])


@router.get("/sources", response_model=FolderSyncSourceList)
async def list_sources(_admin: AdminUser, db: DbSession) -> FolderSyncSourceList:
    return FolderSyncSourceList(items=[FolderSyncSourceOut.model_validate(row) for row in await folder_sync.list_sources(db)])


@router.post("/sources", response_model=FolderSyncSourceOut, status_code=201)
async def create_source(
    payload: FolderSyncSourceCreate,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    admin: AdminUser,
) -> FolderSyncSourceOut:
    row = await folder_sync.create_source(
        db,
        admin,
        settings,
        name=payload.name,
        relative_path=payload.relative_path,
        visibility=payload.visibility,
        department_group_id=payload.department_group_id,
        interval_minutes=payload.interval_minutes,
        ctx=audit_context(request, admin),
    )
    return FolderSyncSourceOut.model_validate(row)


@router.patch("/sources/{source_id}", response_model=FolderSyncSourceOut)
async def update_source(
    source_id: uuid.UUID,
    payload: FolderSyncSourceUpdate,
    request: Request,
    db: DbSession,
    _admin: AdminUser,
) -> FolderSyncSourceOut:
    row = await folder_sync.update_source(
        db,
        source_id,
        interval_minutes=payload.interval_minutes,
        enabled=payload.enabled,
        ctx=audit_context(request, _admin),
    )
    return FolderSyncSourceOut.model_validate(row)


@router.get("/sources/{source_id}/runs", response_model=FolderSyncRunList)
async def list_runs(source_id: uuid.UUID, db: DbSession, _admin: AdminUser) -> FolderSyncRunList:
    return FolderSyncRunList(items=[FolderSyncRunOut.model_validate(row) for row in await folder_sync.list_runs(db, source_id)])


def _notification_out(
    notification: FolderSyncNotification,
    run: FolderSyncRun,
    source: FolderSyncSource,
) -> FolderSyncNotificationOut:
    return FolderSyncNotificationOut(
        id=notification.id,
        source_id=source.id,
        source_name=source.name,
        run_id=run.id,
        status=run.status,  # type: ignore[arg-type]
        created=run.created,
        updated=run.updated,
        removed=run.removed,
        failed=run.failed,
        error_code=run.error_code,
        created_at=notification.created_at,
        read_at=notification.read_at,
    )


@router.get("/notifications", response_model=FolderSyncNotificationList)
async def list_notifications(db: DbSession, user: CurrentUser) -> FolderSyncNotificationList:
    rows = await folder_sync.list_notifications(db, user.id)
    return FolderSyncNotificationList(items=[_notification_out(*row) for row in rows])


@router.post("/notifications/{notification_id}/read", response_model=FolderSyncNotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> FolderSyncNotificationOut:
    notification = await folder_sync.mark_notification_read(db, user.id, notification_id)
    row = await db.execute(
        select(FolderSyncRun, FolderSyncSource)
        .join(FolderSyncSource, FolderSyncSource.id == FolderSyncRun.source_id)
        .where(FolderSyncRun.id == notification.run_id)
    )
    run, source = row.one()
    return _notification_out(notification, run, source)


@router.get("/preferences", response_model=FolderSyncPreferenceOut)
async def get_preferences(db: DbSession, user: CurrentUser) -> FolderSyncPreferenceOut:
    return FolderSyncPreferenceOut(change_alerts_enabled=await folder_sync.get_preference(db, user.id))


@router.put("/preferences", response_model=FolderSyncPreferenceOut)
async def update_preferences(payload: FolderSyncPreferenceIn, db: DbSession, user: CurrentUser) -> FolderSyncPreferenceOut:
    enabled = await folder_sync.set_preference(db, user.id, payload.change_alerts_enabled)
    return FolderSyncPreferenceOut(change_alerts_enabled=enabled)
