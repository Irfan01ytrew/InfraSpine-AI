"""DB-free unit tests for the Step V Drive connector class (slice 4).

No database, no network, no real credentials: every external call goes
through ``MockTransport`` with canned responses. Drive is read-only in the
catalog — nothing here can change remote files.
"""

import asyncio

import pytest

from app.integrations.drive import (
    DriveAuthError,
    DriveConnector,
    DriveRateLimitedError,
)
from app.integrations.registry import ConnectorRegistry, MockTransport, RegistryError

ABOUT = {"user": {"emailAddress": "owner@example.com", "displayName": "Owner"}}
FILES_PAGE = {
    "files": [
        {"id": "f1", "name": "Invoice 7.pdf", "mimeType": "application/pdf", "size": "1234",
         "modifiedTime": "2026-11-20T10:00:00Z", "secret": "must-be-dropped"},
        {"id": "f2", "name": "Notes", "mimeType": "application/vnd.google-apps.folder"},
    ],
    "nextPageToken": "tok-1",
}


def test_drive_catalog_entry_and_scope():
    registry = ConnectorRegistry()
    assert "google_drive" in registry.providers()
    assert registry.supports_operation("google_drive", "verify") is True
    assert registry.supports_operation("google_drive", "sync") is True
    assert registry.supports_operation("google_drive", "email.send") is False
    assert registry.required_scope("google_drive", "verify") is None
    assert registry.required_scope("google_drive", "sync") is None


def test_drive_verify_returns_account_and_maps_auth_error():
    async def run():
        transport = MockTransport()
        transport.when("/about", ABOUT)
        connector = DriveConnector(transport)
        assert await connector.verify() == {
            "ok": True,
            "emailAddress": "owner@example.com",
            "displayName": "Owner",
        }
        denied = MockTransport()
        denied.when("/about", {"error": {"code": 401, "message": "Unauthenticated"}})
        with pytest.raises(DriveAuthError):
            await DriveConnector(denied).verify()
        broken = MockTransport()
        broken.when("/about", {"nope": True})
        with pytest.raises(Exception):
            await DriveConnector(broken).verify()

    asyncio.run(run())


def test_drive_list_files_allow_lists_fields_and_token():
    async def run():
        transport = MockTransport()
        transport.when("/files?pageSize=2", FILES_PAGE)
        connector = DriveConnector(transport)
        result = await connector.list_files(page_size=2)
        assert result == {
            "files": [
                {"id": "f1", "name": "Invoice 7.pdf", "mimeType": "application/pdf",
                 "size": "1234", "modifiedTime": "2026-11-20T10:00:00Z"},
                {"id": "f2", "name": "Notes", "mimeType": "application/vnd.google-apps.folder"},
            ],
            "nextPageToken": "tok-1",
        }
        assert "secret" not in result["files"][0]
        with pytest.raises(Exception):
            await connector.list_files(page_size=0)
        with pytest.raises(Exception):
            await connector.list_files(page_size=101)
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_drive_list_quota_error_is_mapped():
    async def run():
        transport = MockTransport()
        transport.when(
            "/files?pageSize=10",
            {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries'"}},
        )
        with pytest.raises(DriveRateLimitedError):
            await DriveConnector(transport).list_files()

    asyncio.run(run())


def test_drive_sync_delegates_to_list_with_trashed_excluded():
    async def run():
        transport = MockTransport()
        transport.when("/files?pageSize=100&q=trashed = false", FILES_PAGE)
        connector = DriveConnector(transport)
        result = await connector.sync()
        assert [f["id"] for f in result["files"]] == ["f1", "f2"]
        assert result["nextPageToken"] == "tok-1"
        assert transport.calls[0]["method"] == "GET"

    asyncio.run(run())


def test_drive_fetch_file_returns_metadata_and_rejects_bad_id():
    async def run():
        transport = MockTransport()
        transport.when(
            "/files/f1?fields=id,name,mimeType,size,modifiedTime",
            {"id": "f1", "name": "Invoice 7.pdf", "mimeType": "application/pdf"},
        )
        connector = DriveConnector(transport)
        assert await connector.fetch_file("f1") == {
            "id": "f1", "name": "Invoice 7.pdf", "mimeType": "application/pdf",
        }
        with pytest.raises(Exception):
            await connector.fetch_file("../etc/passwd")
        with pytest.raises(Exception):
            await connector.fetch_file("")
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_drive_download_file_returns_content_and_maps_error():
    async def run():
        transport = MockTransport()
        transport.when("/files/f1?alt=media", "PDF-BYTES")
        connector = DriveConnector(transport)
        assert await connector.download_file("f1") == {"ok": True, "id": "f1", "content": "PDF-BYTES"}
        denied = MockTransport()
        denied.when("/files/f1?alt=media", {"error": {"code": 401, "message": "Unauthenticated"}})
        with pytest.raises(DriveAuthError):
            await DriveConnector(denied).download_file("f1")
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_drive_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = DriveConnector(MockTransport())
    event = connector.receive_event({
        "event_id": "chg-1",
        "user": "owner@example.com",
        "text": "Invoice 7.pdf changed",
        "ts": "1700000000",
        "type": "change",
        "role": "admin",
        "approve": True,
        "payee": "attacker",
        "amount": 5000,
    })
    assert event["provider"] == "google_drive"
    assert event["trusted"] is False
    assert event["text"] == "Invoice 7.pdf changed"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_registry_drive_end_to_end_via_mock():
    async def run():
        registry = ConnectorRegistry()
        transport = MockTransport()
        transport.when("/about", ABOUT)
        registry.register("google_drive", DriveConnector(transport))
        connector = registry.resolve("google_drive")
        result = await connector.verify()
        assert result["emailAddress"] == "owner@example.com"
        assert registry.registered() == ["google_drive"]
        with pytest.raises(RegistryError):
            registry.register("google_drive", DriveConnector(transport))

    asyncio.run(run())
