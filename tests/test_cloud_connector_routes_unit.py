"""Database-free route checks for cloud connector sync state and secret handling."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.api.routes import cloud_connectors as routes
from app.core.crypto import CredentialCipher
from app.schemas.cloud_connectors import CloudConnectorCreate, CloudConnectorSyncResult


def _request() -> Request:
    app = SimpleNamespace(state=SimpleNamespace(sessionmaker=MagicMock()))
    return Request({"type": "http", "method": "POST", "path": "/api/connectors/test/sync", "headers": [], "client": ("127.0.0.1", 1234), "app": app})


def _record(master_key: bytes):
    connector_id = uuid4()
    cipher = CredentialCipher(master_key, "unit-key")
    encrypted = cipher.encrypt(
        '{"client_id":"synthetic-id","client_secret":"C16_ONLY_SYNTHETIC_SECRET","refresh_token":"C16_ONLY_SYNTHETIC_REFRESH"}',
        associated_data=f"infraspine:cloud-connector:{connector_id}".encode(),
    )
    return SimpleNamespace(
        id=connector_id,
        name="Unit connector",
        connector_type="google_drive",
        visibility="restricted",
        department_group_id=uuid4(),
        last_cursor="old-synthetic-cursor",
        permission_reconcile_cursor="old-private-reconcile-token",
        credential_ciphertext=encrypted.ciphertext,
        credential_nonce=encrypted.nonce,
        credential_key_id=encrypted.key_id,
    )


def _db(record):
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: record.id),
            SimpleNamespace(),
        ]
    )
    db.scalar = AsyncMock(return_value=record)
    db.get = AsyncMock(return_value=SimpleNamespace(
        id=record.department_group_id, kind="department", source="local", lifecycle_status="active"
    ))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def test_google_connector_requires_refresh_token():
    with pytest.raises(ValidationError):
        CloudConnectorCreate(
            name="Synthetic Google source",
            connector_type="google_drive",
            client_id="synthetic-id",
            client_secret="synthetic-secret",
        )


def test_cloud_connector_requires_restricted_department_scope():
    common = {
        "name": "Synthetic Google source",
        "connector_type": "google_drive",
        "client_id": "synthetic-id",
        "client_secret": "synthetic-secret",
        "refresh_token": "synthetic-refresh-token",
    }
    with pytest.raises(ValidationError):
        CloudConnectorCreate(**common)
    with pytest.raises(ValidationError):
        CloudConnectorCreate(**common, visibility="shared", department_group_id=uuid4())


def test_sync_persists_cursor_only_after_complete_success(monkeypatch):
    async def run():
        key = b"k" * 32
        record = _record(key)
        db = _db(record)
        result = CloudConnectorSyncResult(
            connector_id=record.id,
            status="success",
            files_discovered=2,
            files_synced=2,
            files_deleted=0,
            next_cursor="new-synthetic-cursor",
        )
        monkeypatch.setattr(routes, "build_connector_instance", MagicMock(return_value=object()))
        monkeypatch.setattr(routes, "execute_connector_sync", AsyncMock(return_value=result))
        response = await routes.sync_connector(
            record.id,
            SimpleNamespace(id=uuid4(), email="admin@synthetic.invalid"),
            SimpleNamespace(master_key_bytes=key, master_encryption_key_id="unit-key"),
            _request(),
            db,
        )
        update_values = db.execute.await_args_list[-1].args[0].compile().params
        assert response.status == "success"
        assert update_values["last_cursor"] == "new-synthetic-cursor"
        assert response.model_dump().get("next_cursor") is None
        assert db.commit.await_count == 2

    asyncio.run(run())


def test_partial_sync_keeps_old_cursor_for_retry(monkeypatch):
    async def run():
        key = b"k" * 32
        record = _record(key)
        db = _db(record)
        result = CloudConnectorSyncResult(
            connector_id=record.id,
            status="partial",
            files_discovered=2,
            files_synced=1,
            files_deleted=0,
            errors=["A cloud source file could not be downloaded."],
            next_cursor="must-not-be-committed",
        )
        monkeypatch.setattr(routes, "build_connector_instance", MagicMock(return_value=object()))
        monkeypatch.setattr(routes, "execute_connector_sync", AsyncMock(return_value=result))
        response = await routes.sync_connector(
            record.id,
            SimpleNamespace(id=uuid4(), email="admin@synthetic.invalid"),
            SimpleNamespace(master_key_bytes=key, master_encryption_key_id="unit-key"),
            _request(),
            db,
        )
        update_values = db.execute.await_args_list[-1].args[0].compile().params
        assert response.status == "partial"
        assert "last_cursor" not in update_values
        assert update_values["sync_in_progress"] is False

    asyncio.run(run())


def test_sync_persists_private_reconciliation_cursor_and_reports_bounded_progress(monkeypatch):
    async def run():
        key = b"k" * 32
        record = _record(key)
        db = _db(record)
        result = CloudConnectorSyncResult(
            connector_id=record.id,
            status="success",
            files_discovered=0,
            files_synced=0,
            files_deleted=0,
            next_cursor="new-delta-cursor",
        )

        class Adapter:
            async def fetch_permission_reconciliation_page(self, cursor=None, page_size=50):
                return None

        monkeypatch.setattr(routes, "build_connector_instance", MagicMock(return_value=Adapter()))
        monkeypatch.setattr(routes, "execute_connector_sync", AsyncMock(return_value=result))
        monkeypatch.setattr(
            routes,
            "execute_permission_reconciliation_page",
            AsyncMock(
                return_value=SimpleNamespace(
                    scanned=8,
                    reconciled=6,
                    quarantined=2,
                    failed=0,
                    next_cursor="new-private-reconcile-token",
                    complete=False,
                )
            ),
        )
        response = await routes.sync_connector(
            record.id,
            SimpleNamespace(id=uuid4(), email="admin@synthetic.invalid"),
            SimpleNamespace(master_key_bytes=key, master_encryption_key_id="unit-key"),
            _request(),
            db,
        )
        update_values = db.execute.await_args_list[-1].args[0].compile().params
        serialized = response.model_dump_json()
        assert response.permission_reconciliation_scanned == 8
        assert response.permission_reconciliation_updated == 6
        assert response.permission_reconciliation_quarantined == 2
        assert response.permission_reconciliation_complete is False
        assert update_values["permission_reconcile_cursor"] == "new-private-reconcile-token"
        assert "new-private-reconcile-token" not in serialized
        assert "old-private-reconcile-token" not in serialized

    asyncio.run(run())


def test_sync_failure_returns_generic_error_and_releases_claim(monkeypatch):
    async def run():
        key = b"k" * 32
        record = _record(key)
        db = _db(record)
        monkeypatch.setattr(routes, "build_connector_instance", MagicMock(return_value=object()))
        monkeypatch.setattr(routes, "execute_connector_sync", AsyncMock(side_effect=RuntimeError("C16_ONLY_SYNTHETIC_SECRET")))
        response = await routes.sync_connector(
            record.id,
            SimpleNamespace(id=uuid4(), email="admin@synthetic.invalid"),
            SimpleNamespace(master_key_bytes=key, master_encryption_key_id="unit-key"),
            _request(),
            db,
        )
        update_values = db.execute.await_args_list[-1].args[0].compile().params
        serialized = response.model_dump_json()
        assert response.status == "failed"
        assert "C16_ONLY_SYNTHETIC_SECRET" not in serialized
        assert response.errors == ["The cloud source could not be synchronized."]
        assert update_values["sync_in_progress"] is False
        assert update_values["last_sync_status"] == "failed"
        db.rollback.assert_awaited_once()

    asyncio.run(run())
