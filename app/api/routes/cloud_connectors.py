"""Admin API endpoints for durable cloud storage connector configuration."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select, update

from app.api.deps import AdminUser, AppSettings, DbSession, audit_context, get_current_user
from app.core.crypto import CredentialCipher, EncryptedValue
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import (
    ACL_QUARANTINED,
    SOURCE_FILE_SERVER,
    AccessGroup,
    CloudConnectorConfig,
    CloudSourceAcl,
    Document,
)
from app.schemas.cloud_connectors import (
    CloudConnectorCreate,
    CloudConnectorOut,
    CloudConnectorSyncResult,
)
from app.services import audit
from app.services.cloud_connectors.sync_engine import (
    build_connector_instance,
    execute_connector_sync,
    execute_permission_reconciliation_page,
)

router = APIRouter(prefix="/api/connectors", tags=["cloud connectors"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def _response(record: CloudConnectorConfig) -> CloudConnectorOut:
    return CloudConnectorOut(
        id=record.id,
        name=record.name,
        connector_type=record.connector_type,
        visibility=record.visibility,
        department_group_id=record.department_group_id,
        sync_interval_minutes=record.sync_interval_minutes,
        is_active=record.is_active,
        sync_in_progress=record.sync_in_progress,
        last_sync_status=record.last_sync_status,
        last_sync_error_count=record.last_sync_error_count,
        last_synced_at=record.last_synced_at,
        last_cursor=record.last_cursor,
        created_at=record.created_at,
    )


@router.get("", response_model=list[CloudConnectorOut])
async def list_connectors(_admin: AdminUser, db: DbSession) -> list[CloudConnectorOut]:
    """List connector metadata without credentials or opaque provider cursors."""
    rows = (await db.execute(select(CloudConnectorConfig).order_by(CloudConnectorConfig.created_at.desc()))).scalars()
    return [_response(row) for row in rows]


@router.post("", response_model=CloudConnectorOut, status_code=201)
async def create_connector(
    payload: CloudConnectorCreate,
    _admin: AdminUser,
    settings: AppSettings,
    request: Request,
    db: DbSession,
) -> CloudConnectorOut:
    """Save provider credentials encrypted at rest; provider access is checked on explicit sync."""
    connector_id = uuid4()
    department = await db.get(AccessGroup, payload.department_group_id)
    if (
        department is None
        or department.kind != "department"
        or department.source != "local"
        or department.lifecycle_status != "active"
    ):
        raise InvalidInputError("Select an active, locally managed department for imported documents.")
    credentials = {
        "client_id": payload.client_id,
        "client_secret": payload.client_secret,
        "tenant_id": payload.tenant_id,
        "refresh_token": payload.refresh_token,
        "drive_id": payload.drive_id,
        "folder_id": payload.folder_id,
    }
    cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    encrypted = cipher.encrypt(
        json.dumps(credentials, separators=(",", ":")),
        associated_data=f"infraspine:cloud-connector:{connector_id}".encode(),
    )
    record = CloudConnectorConfig(
        id=connector_id,
        name=payload.name,
        connector_type=payload.connector_type,
        visibility=payload.visibility,
        department_group_id=payload.department_group_id,
        sync_interval_minutes=payload.sync_interval_minutes,
        credential_ciphertext=encrypted.ciphertext,
        credential_nonce=encrypted.nonce,
        credential_key_id=encrypted.key_id,
        created_by=_admin.id,
    )
    db.add(record)
    audit.record(
        db,
        audit_context(request, _admin),
        "cloud_connector.created",
        target=str(connector_id),
        details={"name": payload.name, "type": payload.connector_type},
    )
    await db.commit()
    await db.refresh(record)
    return _response(record)


@router.post("/{connector_id}/sync", response_model=CloudConnectorSyncResult)
async def sync_connector(
    connector_id: UUID,
    _admin: AdminUser,
    settings: AppSettings,
    request: Request,
    db: DbSession,
) -> CloudConnectorSyncResult:
    """Run one explicitly requested sync, preventing overlapping attempts per connector."""
    sync_audit_ctx = audit_context(request, _admin)
    claimed = await db.execute(
        update(CloudConnectorConfig)
        .where(CloudConnectorConfig.id == connector_id, CloudConnectorConfig.sync_in_progress.is_(False))
        .values(sync_in_progress=True)
        .returning(CloudConnectorConfig.id)
    )
    if claimed.scalar_one_or_none() is None:
        exists = await db.scalar(select(CloudConnectorConfig.id).where(CloudConnectorConfig.id == connector_id))
        if exists is None:
            raise NotFoundError("Cloud connector not found.")
        raise ConflictError("A sync is already running for this connector.")
    await db.commit()

    record = await db.scalar(
        select(CloudConnectorConfig).where(CloudConnectorConfig.id == connector_id).with_for_update()
    )
    if record is None:  # Deleted between claim and read; retain a safe not-found response.
        raise NotFoundError("Cloud connector not found.")

    try:
        department = await db.get(AccessGroup, record.department_group_id) if record.department_group_id else None
        if (
            record.visibility != "restricted"
            or department is None
            or department.kind != "department"
            or department.source != "local"
            or department.lifecycle_status != "active"
        ):
            raise InvalidInputError("The cloud connector department scope is unavailable.")
        cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
        credentials = json.loads(
            cipher.decrypt(
                EncryptedValue(
                    ciphertext=record.credential_ciphertext,
                    nonce=record.credential_nonce,
                    key_id=record.credential_key_id,
                ),
                associated_data=f"infraspine:cloud-connector:{connector_id}".encode(),
            )
        )
        adapter = build_connector_instance(record.connector_type, credentials)
        result = await execute_connector_sync(
            connector=adapter,
            connector_id=connector_id,
            connector_name=record.name,
            visibility=record.visibility,
            department_group_id=record.department_group_id,
            cursor=record.last_cursor,
            settings=settings,
            db=db,
            audit_ctx=sync_audit_ctx,
            sessionmaker=request.app.state.sessionmaker,
            admin_email=_admin.email,
        )
        reconciliation_values = {}
        reconciliation_method = getattr(adapter, "fetch_permission_reconciliation_page", None)
        if callable(reconciliation_method):
            try:
                reconciliation = await execute_permission_reconciliation_page(
                    connector=adapter,
                    connector_id=connector_id,
                    cursor=getattr(record, "permission_reconcile_cursor", None),
                    db=db,
                )
                reconciliation_values = {
                    "permission_reconcile_cursor": None if reconciliation.complete else reconciliation.next_cursor,
                }
                result = result.model_copy(
                    update={
                        "permission_reconciliation_scanned": reconciliation.scanned,
                        "permission_reconciliation_updated": reconciliation.reconciled,
                        "permission_reconciliation_quarantined": reconciliation.quarantined,
                        "permission_reconciliation_failed": reconciliation.failed,
                        "permission_reconciliation_complete": reconciliation.complete,
                    }
                )
                if reconciliation.failed:
                    result.errors.append("Some source permissions were quarantined for a later retry.")
                    if result.status == "success":
                        result.status = "partial"
                audit.record(
                    db,
                    sync_audit_ctx,
                    "cloud_connector.permissions_reconciled",
                    target=str(connector_id),
                    details={
                        "scanned": reconciliation.scanned,
                        "updated": reconciliation.reconciled,
                        "quarantined": reconciliation.quarantined,
                        "failed": reconciliation.failed,
                        "complete": reconciliation.complete,
                    },
                )
            except NotImplementedError:
                # Third-party test adapters may support delta sync but not full inventory scans.
                pass
            except Exception as exc:
                # Keep the old private page cursor so this bounded page can be retried.
                result.errors.append("The cloud permission inventory page could not be reconciled.")
                if result.status == "success":
                    result.status = "partial"
                logger.warning("Cloud permission inventory page failed error_type=%s", type(exc).__name__)
        values = {
            "sync_in_progress": False,
            "last_synced_at": datetime.now(UTC),
            "last_sync_status": result.status,
            "last_sync_error_count": len(result.errors),
            **reconciliation_values,
        }
        # Keep the old delta cursor after partial failures so a retry revisits undownloaded files.
        if result.status == "success" and result.next_cursor:
            values["last_cursor"] = result.next_cursor
        await db.execute(update(CloudConnectorConfig).where(CloudConnectorConfig.id == connector_id).values(**values))
        await db.commit()
        return result
    except Exception as exc:
        await db.rollback()
        # Persist a generic status only; provider exception text can contain credentials or file names.
        await db.execute(
            update(CloudConnectorConfig)
            .where(CloudConnectorConfig.id == connector_id)
            .values(sync_in_progress=False, last_sync_status="failed", last_sync_error_count=1)
        )
        audit.record(
            db,
            sync_audit_ctx,
            "cloud_connector.sync.failed",
            outcome="failure",
            target=str(connector_id),
            details={"error_type": type(exc).__name__},
        )
        await db.commit()
        return CloudConnectorSyncResult(
            connector_id=connector_id,
            status="failed",
            files_discovered=0,
            files_synced=0,
            files_deleted=0,
            errors=["The cloud source could not be synchronized."],
        )


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: UUID,
    _admin: AdminUser,
    request: Request,
    db: DbSession,
) -> None:
    """Remove a connector and its encrypted credentials."""
    record = await db.scalar(
        select(CloudConnectorConfig).where(CloudConnectorConfig.id == connector_id).with_for_update()
    )
    if record is None:
        raise NotFoundError("Cloud connector not found.")
    if record.sync_in_progress:
        raise ConflictError("This connector cannot be removed while a sync is running.")
    # Keep imported originals and their versions, but revoke their access when the
    # connector (and its ability to refresh source ACL scope) is removed.
    await db.execute(
        update(Document)
        .where(
            Document.source_system == SOURCE_FILE_SERVER,
            Document.source_uri.like(f"cloud://{connector_id}/%"),
            Document.acl_status != ACL_QUARANTINED,
        )
        .values(acl_status=ACL_QUARANTINED, acl_quarantine_reason="source_removed")
    )
    await db.execute(
        delete(CloudSourceAcl).where(
            CloudSourceAcl.document_id.in_(
                select(Document.id).where(
                    Document.source_system == SOURCE_FILE_SERVER,
                    Document.source_uri.like(f"cloud://{connector_id}/%"),
                )
            )
        )
    )
    await db.delete(record)
    audit.record(
        db,
        audit_context(request, _admin),
        "cloud_connector.deleted",
        target=str(connector_id),
        details={},
    )
    await db.commit()
