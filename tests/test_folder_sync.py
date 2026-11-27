from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, FolderSyncNotification, FolderSyncRun, FolderSyncSource
from app.main import app
from app.rag.bulk_import import import_folder
from app.services.folder_sync import _claim_source, process_folder_sync_once
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, login


def _create_source(client, *, relative_path: str = "engineering", department_id: str | None = None):
    return client.post(
        "/api/folder-sync/sources",
        json={
            "name": "Engineering handbook",
            "relative_path": relative_path,
            "visibility": "restricted" if department_id else "shared",
            "department_group_id": department_id,
            "interval_minutes": 5,
        },
    )


def _run_due(client):
    return client.portal.call(
        process_folder_sync_once,
        app.state.sessionmaker,
        get_settings(),
    )


def _make_due(client, source_id: str):
    async def update():
        async with app.state.sessionmaker() as db:
            source = await db.scalar(select(FolderSyncSource).where(FolderSyncSource.id == source_id))
            source.next_sync_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

    client.portal.call(update)


def _counts(client, source_id: str):
    async def get():
        async with app.state.sessionmaker() as db:
            runs = list(
                (
                    await db.scalars(
                        select(FolderSyncRun)
                        .join(FolderSyncSource, FolderSyncSource.id == FolderSyncRun.source_id)
                        .where(FolderSyncSource.id == source_id)
                        .order_by(FolderSyncRun.started_at)
                    )
                ).all()
            )
            source = await db.get(FolderSyncSource, source_id)
            return runs, source

    return client.portal.call(get)


def test_folder_sync_admin_schedule_diffs_notifications_acl_and_retry_recovery(client, tmp_path: Path, monkeypatch):
    root = tmp_path / "import-root"
    source_folder = root / "engineering"
    source_folder.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "rag_import_root", root)
    document_path = source_folder / "ops.txt"
    document_path.write_text("Pump room operations are reviewed each quarter.", encoding="utf-8")

    login(client, USER2_EMAIL)
    assert client.get("/api/folder-sync/sources").status_code == 403
    assert client.get("/api/folder-sync/preferences").json() == {"change_alerts_enabled": True}
    assert client.put("/api/folder-sync/preferences", json={"change_alerts_enabled": False}).status_code == 200
    assert client.get("/api/folder-sync/preferences").json() == {"change_alerts_enabled": False}

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    group_response = client.post("/api/admin/access/groups", json={"name": "Scheduled Sync Department", "kind": "department"})
    assert group_response.status_code == 201, group_response.text
    department_id = group_response.json()["id"]
    assert client.post(
        f"/api/admin/access/groups/{department_id}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    member_id = client.get(f"/api/admin/access/groups/{department_id}/members").json()["members"][0]["user_id"]
    assert _create_source(client, relative_path="../outside").status_code == 422
    created = _create_source(client, department_id=department_id)
    assert created.status_code == 201, created.text
    source = created.json()
    assert source["owner_user_id"] and source["enabled"] is True
    assert _run_due(client) is True
    first_run = client.get(f"/api/folder-sync/sources/{source['id']}/runs").json()["items"][0]
    assert first_run["status"] == "completed"
    assert (first_run["scanned"], first_run["created"], first_run["updated"], first_run["removed"]) == (1, 1, 0, 0)
    notices = client.get("/api/folder-sync/notifications").json()["items"]
    assert len(notices) == 1 and notices[0]["created"] == 1 and notices[0]["source_name"] == "Engineering handbook"
    document_id = client.get("/api/rag/documents").json()["documents"][0]["id"]

    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get("/api/folder-sync/notifications").json()["items"] == []
    assert client.post(f"/api/folder-sync/notifications/{notices[0]['id']}/read").status_code == 404
    assert client.get(f"/api/rag/documents/{document_id}").status_code == 200

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    marked = client.post(f"/api/folder-sync/notifications/{notices[0]['id']}/read")
    assert marked.status_code == 200 and marked.json()["read_at"] is not None
    assert client.put("/api/folder-sync/preferences", json={"change_alerts_enabled": False}).json() == {
        "change_alerts_enabled": False
    }
    assert client.delete(f"/api/admin/access/groups/{department_id}/members/{member_id}").status_code == 204
    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_id}").status_code == 404
    client.cookies.clear()
    login(client, ADMIN_EMAIL)

    # Turning alerts off is per-user; the scheduled import continues while notices are suppressed.
    document_path.write_text("Pump room inspection is reviewed every month.", encoding="utf-8")
    _make_due(client, source["id"])
    assert _run_due(client) is True
    runs, persisted_source = _counts(client, source["id"])
    assert runs[1].status == "completed" and runs[1].updated == 1
    assert persisted_source.consecutive_failures == 0 and persisted_source.last_error_code is None
    after_preference_change = client.get("/api/folder-sync/notifications").json()["items"]
    assert len(after_preference_change) == 1
    assert after_preference_change[0]["read_at"] is not None
    assert after_preference_change[0]["run_id"] == first_run["id"]
    current = client.get(f"/api/rag/documents/{document_id}").json()
    assert current["version"] == 2

    # A successful complete scan quarantines missing source documents without deleting stored content.
    document_path.unlink()
    _make_due(client, source["id"])
    assert _run_due(client) is True
    runs, _ = _counts(client, source["id"])
    assert runs[2].status == "completed" and runs[2].removed == 1
    async def read_document():
        async with app.state.sessionmaker() as db:
            return await db.get(Document, document_id)

    quarantined = client.portal.call(read_document)
    assert quarantined.acl_status == "quarantined"
    assert client.get(f"/api/rag/documents/{document_id}").status_code == 404
    after_removal = client.get("/api/folder-sync/notifications").json()["items"]
    assert len(after_removal) == 1 and after_removal[0]["run_id"] == first_run["id"]

    # Missing mounted source path creates a retryable failure; restoring it resumes without losing history.
    document_path.write_text("Pump room operations are back under review.", encoding="utf-8")
    source_folder.rename(root / "temporarily-unavailable")
    _make_due(client, source["id"])
    assert _run_due(client) is True
    runs, persisted_source = _counts(client, source["id"])
    assert runs[3].status == "failed" and persisted_source.consecutive_failures == 1
    assert persisted_source.next_sync_at is not None and persisted_source.last_error_code == "InvalidInputError"
    (root / "temporarily-unavailable").rename(source_folder)
    _make_due(client, source["id"])
    assert _run_due(client) is True
    runs, persisted_source = _counts(client, source["id"])
    assert runs[4].status == "completed" and runs[4].updated == 1
    assert persisted_source.consecutive_failures == 0 and persisted_source.last_error_code is None
    restored_document = client.portal.call(read_document)
    assert restored_document.acl_status == "active"
    assert client.get(f"/api/rag/documents/{document_id}").status_code == 200

    # Admin can pause a source; the worker leaves disabled schedules untouched.
    disabled = client.patch(f"/api/folder-sync/sources/{source['id']}", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    _make_due(client, source["id"])
    assert _run_due(client) is False


def test_folder_sync_recovers_abandoned_lease_after_partial_import_and_alerts(client, tmp_path: Path, monkeypatch):
    root = tmp_path / "import-root"
    source_folder = root / "recovery"
    source_folder.mkdir(parents=True)
    first_file = source_folder / "first.txt"
    first_file.write_text("Synthetic first recovery source.", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "rag_import_root", root)

    login(client, ADMIN_EMAIL)
    created = _create_source(client, relative_path="recovery")
    assert created.status_code == 201, created.text
    source = created.json()

    async def import_first_file_then_abandon_run():
        now = datetime.now(UTC)
        claimed = await _claim_source(app.state.sessionmaker, now)
        assert claimed is not None
        source_id, run_id = claimed
        stats = await import_folder(
            app.state.sessionmaker,
            get_settings(),
            source_folder,
            admin_email=ADMIN_EMAIL,
            visibility="shared",
            department_id=None,
            source_id=source["source_id"],
            run_id=run_id,
            detect_removed=True,
        )
        async with app.state.sessionmaker() as db:
            persisted_source = await db.get(FolderSyncSource, source_id)
            abandoned_run = await db.get(FolderSyncRun, run_id)
            assert stats["created"] == 1 and stats["scanned"] == 1
            assert abandoned_run.status == "running"
            persisted_source.next_sync_at = now + timedelta(minutes=10)
            persisted_source.locked_until = now - timedelta(seconds=1)
            await db.commit()
        return run_id

    abandoned_id = client.portal.call(import_first_file_then_abandon_run)
    (source_folder / "second.txt").write_text("Synthetic second recovery source.", encoding="utf-8")
    recovery_time = datetime.now(UTC)

    async def recover_abandoned_run():
        return await process_folder_sync_once(
            app.state.sessionmaker,
            get_settings(),
            now=recovery_time,
        )

    assert client.portal.call(recover_abandoned_run) is True

    runs, persisted_source = _counts(client, source["id"])
    abandoned = next(run for run in runs if run.id == abandoned_id)
    recovered = next(run for run in runs if run.id != abandoned_id)
    assert (abandoned.status, abandoned.error_code, abandoned.attempt) == ("failed", "lease_expired", 1)
    assert (recovered.status, recovered.attempt, recovered.scanned, recovered.created, recovered.unchanged) == (
        "completed", 2, 2, 1, 1
    )
    assert persisted_source.consecutive_failures == 0 and persisted_source.last_error_code is None
    assert persisted_source.locked_until is None and persisted_source.locked_run_id is None
    assert len(client.get("/api/rag/documents").json()["documents"]) == 2

    async def read_notifications():
        async with app.state.sessionmaker() as db:
            return list(
                (
                    await db.scalars(
                        select(FolderSyncNotification).where(
                            FolderSyncNotification.run_id.in_([abandoned_id, recovered.id])
                        )
                    )
                ).all()
            )

    notifications = client.portal.call(read_notifications)
    assert {item.run_id for item in notifications} == {abandoned_id, recovered.id}
    assert client.get(f"/api/folder-sync/sources/{source['id']}/runs").status_code == 200

    # A subsequent unchanged scan is idempotent and cannot create duplicate documents or notices.
    _make_due(client, source["id"])
    assert _run_due(client) is True
    runs, _ = _counts(client, source["id"])
    assert runs[-1].status == "completed" and runs[-1].created == 0 and runs[-1].unchanged == 2
    assert len(client.portal.call(read_notifications)) == 2


def test_folder_sync_rejects_directory_symlink_escape(client, tmp_path: Path, monkeypatch):
    root = tmp_path / "import-root"
    root.mkdir()
    outside = tmp_path / "outside-import-root"
    outside.mkdir()
    monkeypatch.setattr(get_settings(), "rag_import_root", root)
    link = root / "escape-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        # Creating Windows symlinks needs Developer Mode or SeCreateSymbolicLinkPrivilege.
        # Keep the security assertion active where the host permits creating the test fixture.
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows token lacks permission to create a directory symlink fixture.")
        raise

    login(client, ADMIN_EMAIL)
    assert _create_source(client, relative_path="escape-link").status_code == 422

