"""Focused database-free tests for bounded provider permission reconciliation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx

from app.services.cloud_connectors.base import (
    CloudItem,
    CloudPrincipalPermission,
    PermissionReconciliationPage,
)
from app.services.cloud_connectors.google_drive import (
    GoogleDriveConnector,
    _decode_folder_reconciliation_cursor,
)
from app.services.cloud_connectors.sharepoint_onedrive import MicrosoftGraphConnector
from app.services.cloud_connectors.sync_engine import execute_permission_reconciliation_page


def test_google_inventory_reconciliation_is_bounded_and_resumable():
    async def run():
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            httpx.Response(
                200,
                json={
                    "nextPageToken": "opaque-page-2",
                    "files": [{"id": "file-a", "name": "A.pdf", "mimeType": "application/pdf"}],
                },
            ),
            httpx.Response(
                200,
                json={"files": [{"id": "file-b", "name": "B.docx", "mimeType": "application/pdf"}]},
            ),
        ]
        connector = GoogleDriveConnector(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            refresh_token="synthetic-refresh",
            folder_id="folder'quoted",
            drive_id="drive-synthetic",
            client=client,
        )
        connector._access_token = "synthetic-access"

        first = await connector.fetch_permission_reconciliation_page(page_size=500)
        second = await connector.fetch_permission_reconciliation_page(first.next_cursor, page_size=500)

        params = client.get.await_args_list[0].kwargs["params"]
        saved_cursor = _decode_folder_reconciliation_cursor(first.next_cursor, "folder'quoted")
        second_params = client.get.await_args_list[1].kwargs["params"]
        assert params["pageSize"] == 100
        assert params["corpora"] == "drive" and params["driveId"] == "drive-synthetic"
        assert "folder\\'quoted" in params["q"]
        assert first.items[0].id == "file-a" and first.next_cursor is not None
        assert saved_cursor[0]["page_token"] == "opaque-page-2"
        assert second_params["pageToken"] == "opaque-page-2"
        assert not first.complete and second.complete and second.next_cursor is None
        assert "opaque-page-2" not in repr(first.items) and "opaque-page-2" not in first.next_cursor

    asyncio.run(run())


def test_google_folder_permission_reconciliation_walks_nested_folders_depth_first():
    async def run():
        folder_mime = "application/vnd.google-apps.folder"
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            httpx.Response(
                200,
                json={
                    "nextPageToken": "root-page-2",
                    "files": [
                        {"id": "root-file", "name": "Root.pdf", "mimeType": "application/pdf"},
                        {"id": "child-a", "name": "A", "mimeType": folder_mime},
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "files": [
                        {"id": "child-a-file", "name": "A.docx", "mimeType": "application/pdf"},
                        {"id": "grandchild", "name": "Nested", "mimeType": folder_mime},
                    ]
                },
            ),
            httpx.Response(
                200,
                json={"files": [{"id": "grandchild-file", "name": "Nested.pdf", "mimeType": "application/pdf"}]},
            ),
            httpx.Response(
                200,
                json={
                    "files": [
                        {"id": "root-page-2-file", "name": "Root2.pdf", "mimeType": "application/pdf"},
                        {"id": "child-b", "name": "B", "mimeType": folder_mime},
                    ]
                },
            ),
            httpx.Response(
                200,
                json={"files": [{"id": "child-b-file", "name": "B.pdf", "mimeType": "application/pdf"}]},
            ),
        ]
        connector = GoogleDriveConnector(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            refresh_token="synthetic-refresh",
            folder_id="root-folder",
            client=client,
        )
        connector._access_token = "synthetic-access"

        pages = []
        cursor = None
        for _ in range(5):
            page = await connector.fetch_permission_reconciliation_page(cursor, page_size=10)
            pages.append(page)
            cursor = page.next_cursor

        assert pages[-1].complete and pages[-1].next_cursor is None
        assert [item.id for page in pages for item in page.items if not item.is_folder] == [
            "root-file",
            "child-a-file",
            "grandchild-file",
            "root-page-2-file",
            "child-b-file",
        ]
        queries = [call.kwargs["params"]["q"] for call in client.get.await_args_list]
        assert [query.rsplit("' in parents", 1)[0].split(" and '")[-1] for query in queries] == [
            "root-folder", "child-a", "grandchild", "root-folder", "child-b"
        ]
        root_page_two = client.get.await_args_list[3].kwargs["params"]
        assert root_page_two["pageToken"] == "root-page-2"
        assert "root-page-2" not in repr(pages[0].items)

    asyncio.run(run())


def test_google_folder_reconciliation_accepts_existing_root_page_cursor():
    async def run():
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = httpx.Response(
            200,
            json={
                "nextPageToken": "continued-page-token",
                "files": [{"id": "legacy-file", "name": "Legacy.pdf", "mimeType": "application/pdf"}],
            },
        )
        connector = GoogleDriveConnector(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            refresh_token="synthetic-refresh",
            folder_id="root-folder",
            client=client,
        )
        connector._access_token = "synthetic-access"

        page = await connector.fetch_permission_reconciliation_page("legacy-root-token")

        params = client.get.await_args.kwargs["params"]
        saved_state = _decode_folder_reconciliation_cursor(page.next_cursor, "root-folder")
        assert params["pageToken"] == "legacy-root-token"
        assert page.items[0].id == "legacy-file" and not page.complete
        assert saved_state[0]["page_token"] == "continued-page-token"
        assert "continued-page-token" not in page.next_cursor

    asyncio.run(run())


def test_graph_inventory_reconciliation_ignores_tombstones_and_terminal_delta_token():
    async def run():
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = httpx.Response(
            200,
            json={
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=terminal-token",
                "value": [
                    {"id": "present", "name": "Present.pdf", "file": {"mimeType": "application/pdf"}},
                    {"id": "removed", "@removed": {"reason": "deleted"}},
                ],
            },
        )
        connector = MicrosoftGraphConnector(
            tenant_id="synthetic-tenant",
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            drive_id="d1",
            client=client,
        )
        connector._access_token = "synthetic-access"
        page = await connector.fetch_permission_reconciliation_page()
        assert [item.id for item in page.items] == ["present"]
        assert page.complete and page.next_cursor is None
        assert "terminal-token" not in repr(page)

    asyncio.run(run())


def test_reconciliation_never_revokes_items_missing_from_inventory(monkeypatch):
    async def run():
        existing = SimpleNamespace(
            id=uuid4(), acl_status="active", acl_quarantine_reason=None, acl_synced_at=None
        )
        db = MagicMock()
        db.scalars = AsyncMock(
            side_effect=[SimpleNamespace(all=lambda: [existing]), SimpleNamespace(all=lambda: [])]
        )
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()
        connector = AsyncMock()
        connector.fetch_permission_reconciliation_page.return_value = PermissionReconciliationPage(
            items=[
                CloudItem(id="present-item", name="A.pdf", mime_type="application/pdf"),
                CloudItem(id="inventory-only", name="B.pdf", mime_type="application/pdf"),
            ],
            next_cursor="opaque-next-page",
            complete=False,
        )
        connector.fetch_item_permissions.return_value = [
            CloudPrincipalPermission("user", email="qa@example.test")
        ]
        monkeypatch.setattr(
            "app.services.cloud_connectors.sync_engine._resolve_provider_permissions",
            AsyncMock(return_value=[("user", uuid4())]),
        )
        result = await execute_permission_reconciliation_page(
            connector=connector, connector_id=uuid4(), cursor=None, db=db
        )
        assert (result.scanned, result.reconciled, result.quarantined, result.failed) == (2, 1, 0, 0)
        assert result.next_cursor == "opaque-next-page" and not result.complete
        assert existing.acl_status == "active" and existing.acl_quarantine_reason is None
        assert connector.fetch_item_permissions.await_count == 1
        assert db.commit.await_count == 2

    asyncio.run(run())


def test_transient_permission_failure_quarantines_then_recovers_on_later_pass(monkeypatch):
    async def run():
        existing = SimpleNamespace(
            id=uuid4(), acl_status="active", acl_quarantine_reason=None, acl_synced_at=None
        )
        db = MagicMock()
        db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [existing]))
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()
        connector = AsyncMock()
        connector.fetch_permission_reconciliation_page.side_effect = [
            PermissionReconciliationPage(
                [CloudItem(id="item-1", name="A.pdf", mime_type="application/pdf")],
                "opaque-page-2",
                False,
            ),
            PermissionReconciliationPage(
                [CloudItem(id="item-1", name="A.pdf", mime_type="application/pdf")], None, True
            ),
        ]
        connector.fetch_item_permissions.side_effect = [
            RuntimeError("synthetic provider timeout"),
            [CloudPrincipalPermission("user", email="qa@example.test")],
        ]
        monkeypatch.setattr(
            "app.services.cloud_connectors.sync_engine._resolve_provider_permissions",
            AsyncMock(return_value=[("user", uuid4())]),
        )
        first = await execute_permission_reconciliation_page(
            connector=connector, connector_id=uuid4(), cursor=None, db=db
        )
        assert first.failed == 1 and first.next_cursor == "opaque-page-2"
        assert existing.acl_status == "quarantined"
        assert existing.acl_quarantine_reason == "source_permissions_unavailable"

        second = await execute_permission_reconciliation_page(
            connector=connector, connector_id=uuid4(), cursor=None, db=db
        )
        assert second.reconciled == 1 and second.complete
        assert existing.acl_status == "active" and existing.acl_quarantine_reason is None

    asyncio.run(run())
