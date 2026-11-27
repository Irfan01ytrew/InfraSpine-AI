"""Unit and API tests for Cloud Storage Connectors (Google Drive, OneDrive, SharePoint)."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.core.config import get_settings
from app.core.errors import InvalidInputError
from app.db.models import CloudConnectorConfig, CloudSourceAcl, Document, ResourceAcl
from app.main import app
from app.rag.ingestion import IngestionWorker
from app.services.cloud_connectors.base import (
    CloudItem,
    CloudPrincipalPermission,
    DeltaSyncResult,
)
from app.services.cloud_connectors.google_drive import GoogleDriveConnector
from app.services.cloud_connectors.sharepoint_onedrive import MicrosoftGraphConnector
from app.services.cloud_connectors.sync_engine import (
    build_connector_instance,
    execute_connector_sync,
)
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login
from tests.rag_helpers import activate_provider, ask


def test_google_drive_delta_and_acl_mapping():
    async def _run():
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # Mock OAuth response
        mock_client.post.return_value = httpx.Response(
            200, json={"access_token": "ya29.synthetic_token", "expires_in": 3600}
        )

        # Mock Changes response
        mock_client.get.return_value = httpx.Response(
            200,
            json={
                "nextPageToken": None,
                "newStartPageToken": "cursor_v2",
                "changes": [
                    {
                        "fileId": "file_123",
                        "removed": False,
                        "file": {
                            "id": "file_123",
                            "name": "Q3_Report.pdf",
                            "mimeType": "application/pdf",
                            "size": "1048576",
                            "modifiedTime": "2026-10-27T10:00:00Z",
                            "permissions": [
                                {"emailAddress": "auditor@company.com", "role": "reader"}
                            ],
                        },
                    },
                    {"fileId": "file_deleted", "removed": True},
                ],
            },
        )

        connector = GoogleDriveConnector(
            client_id="g_client_1",
            client_secret="g_secret_1",
            refresh_token="g_refresh_1",
            client=mock_client,
        )

        delta = await connector.fetch_delta(cursor="cursor_v1")
        assert delta.next_cursor == "cursor_v2"
        assert len(delta.items) == 2
        assert "file_deleted" in delta.deleted_ids

        active_item = delta.items[0]
        assert active_item.id == "file_123"
        assert active_item.name == "Q3_Report.pdf"
        assert active_item.remote_acls == ["auditor@company.com"]

        # Verify ACL mapping
        acl = connector.map_acls(active_item.remote_acls)
        assert acl["visibility"] == "restricted"
        assert "auditor@company.com" in acl["allowed_emails"]

    asyncio.run(_run())


def test_microsoft_graph_delta_and_deletion():
    async def _run():
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # Mock Azure AD OAuth
        mock_client.post.return_value = httpx.Response(
            200, json={"access_token": "ey.synthetic_azure_token", "expires_in": 3600}
        )

        # Mock Graph delta items
        mock_client.get.return_value = httpx.Response(
            200,
            json={
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=d_tok_2",
                "value": [
                    {
                        "id": "ms_item_1",
                        "name": "Architecture.docx",
                        "size": 524288,
                        "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                        "lastModifiedDateTime": "2026-10-26T12:00:00Z",
                        "permissions": [
                            {"grantedToV2": {"user": {"email": "engineer@company.com"}}}
                        ],
                    },
                    {
                        "id": "ms_item_removed",
                        "@removed": {"reason": "deleted"},
                    },
                ],
            },
        )

        connector = MicrosoftGraphConnector(
            tenant_id="tenant_abc",
            client_id="ms_client_1",
            client_secret="ms_secret_1",
            drive_id="d1",
            client=mock_client,
        )

        delta = await connector.fetch_delta(cursor=None)
        assert delta.next_cursor.endswith("token=d_tok_2")
        assert len(delta.items) == 2
        assert "ms_item_removed" in delta.deleted_ids

        file_item = delta.items[0]
        assert file_item.id == "ms_item_1"
        assert file_item.name == "Architecture.docx"
        assert file_item.remote_acls == ["engineer@company.com"]

    asyncio.run(_run())


def test_google_drive_live_permissions_preserve_unknown_principals_for_fail_closed_mapping():
    async def _run():
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200,
            json={
                "permissions": [
                    {"id": "directory-user-42", "type": "user", "role": "reader", "emailAddress": "qa@example.test"},
                    {"id": "directory-group-9", "type": "group", "role": "writer"},
                    {"id": "anyone", "type": "anyone", "role": "reader"},
                ]
            },
        )
        connector = GoogleDriveConnector(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            refresh_token="synthetic-refresh",
            client=mock_client,
        )
        connector._access_token = "synthetic-access"
        permissions = await connector.fetch_item_permissions("synthetic-item")
        assert [(p.principal_type, p.external_id, p.email, p.rights) for p in permissions] == [
            ("user", "directory-user-42", "qa@example.test", "read"),
            ("group", "directory-group-9", None, "modify"),
            ("anyone", "anyone", None, "read"),
        ]
        request = mock_client.get.await_args
        assert request.args[0].endswith("/files/synthetic-item/permissions")
        assert "permissions(id,type,role,emailAddress,deleted," in request.kwargs["params"]["fields"]
        assert "permissionDetails(inherited,inheritedFrom,permissionType,role)" in request.kwargs["params"]["fields"]

    asyncio.run(_run())


def test_google_drive_permissions_follow_all_pages():
    async def _run():
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            token = request.url.params.get("pageToken")
            if token is None:
                return httpx.Response(
                    200,
                    json={
                        "permissions": [
                            {"id": "user-1", "type": "user", "role": "reader", "emailAddress": USER_EMAIL}
                        ],
                        "nextPageToken": "page-2",
                    },
                )
            if token == "page-2":
                return httpx.Response(
                    200,
                    json={
                        "permissions": [
                            {"id": "group-2", "type": "group", "role": "reader"}
                        ]
                    },
                )
            return httpx.Response(200, json={"permissions": [], "nextPageToken": "page-2"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = GoogleDriveConnector(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                refresh_token="synthetic-refresh",
                client=client,
            )
            connector._access_token = "synthetic-access"
            permissions = await connector.fetch_item_permissions("synthetic-item")
            assert [permission.external_id for permission in permissions] == ["user-1", "group-2"]
            assert len(requests) == 2
            assert requests[1].url.params["pageToken"] == "page-2"
            assert all(request.url.params["supportsAllDrives"] == "true" for request in requests)

    asyncio.run(_run())


def test_google_drive_permissions_reject_repeated_page_token():
    async def _run():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"permissions": [], "nextPageToken": request.url.params.get("pageToken") or "repeat"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = GoogleDriveConnector(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                refresh_token="synthetic-refresh",
                client=client,
            )
            connector._access_token = "synthetic-access"
            try:
                await connector.fetch_item_permissions("synthetic-item")
            except InvalidInputError:
                pass
            else:
                raise AssertionError("Repeated Google permission page token was not rejected.")

    asyncio.run(_run())


def test_graph_permission_pagination_uses_trusted_same_endpoint_and_rejects_external_link():
    async def _run():
        requests = []
        base = "https://graph.microsoft.com/v1.0/drives/drive-1/items/item-1/permissions"

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "value": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1", "email": USER_EMAIL}}}],
                        "@odata.nextLink": f"{base}?$skiptoken=page2",
                    },
                )
            if request.url.params.get("$skiptoken") == "page2":
                return httpx.Response(
                    200,
                    json={"value": [{"roles": ["write"], "grantedToV2": {"group": {"id": "g2"}}}]},
                )
            return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://attacker.example/steal"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = MicrosoftGraphConnector(
                tenant_id="synthetic-tenant",
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                drive_id="drive-1",
                client=client,
            )
            connector._access_token = "synthetic-access"
            permissions = await connector.fetch_item_permissions("item-1")
            assert [(permission.principal_type, permission.external_id) for permission in permissions] == [
                ("user", "u1"), ("group", "g2")
            ]
            assert len(requests) == 2
            assert all(request.headers["authorization"] == "Bearer synthetic-access" for request in requests)

            malicious = MicrosoftGraphConnector(
                tenant_id="synthetic-tenant",
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                drive_id="drive-1",
                client=client,
            )
            malicious._access_token = "synthetic-access"
            try:
                await malicious.fetch_item_permissions("item-1")
            except InvalidInputError:
                pass
            else:
                raise AssertionError("External Graph permission continuation was not rejected.")
            assert all("attacker.example" not in str(request.url) for request in requests)

    asyncio.run(_run())


def test_graph_live_permissions_capture_link_sharing_as_unsupported():
    async def _run():
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200,
            json={
                "value": [
                    {"id": "p-user", "roles": ["read"], "grantedToV2": {"user": {"id": "aad-8", "userPrincipalName": "qa@example.test"}}},
                    {"id": "p-link", "roles": ["read"], "link": {"type": "anonymous", "scope": "anonymous"}},
                ]
            },
        )
        connector = MicrosoftGraphConnector(
            tenant_id="synthetic-tenant",
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            drive_id="synthetic-drive",
            client=mock_client,
        )
        connector._access_token = "synthetic-access"
        permissions = await connector.fetch_item_permissions("synthetic-item")
        assert [(p.principal_type, p.external_id, p.email) for p in permissions] == [
            ("user", "aad-8", "qa@example.test"),
            ("link", None, None),
        ]
        assert mock_client.get.await_args.args[0].endswith("/items/synthetic-item/permissions")

    asyncio.run(_run())


def test_cloud_sync_engine_stages_files_and_audits(tmp_path, monkeypatch):
    async def _run():
        mock_connector = AsyncMock()
        mock_connector.fetch_delta.return_value = DeltaSyncResult(
            items=[
                CloudItem(
                    id="c_doc_1",
                    name="Security_Policy.pdf",
                    mime_type="application/pdf",
                    size_bytes=1000,
                    is_deleted=False,
                )
            ],
            next_cursor="c_next_1",
            deleted_ids=[],
        )
        mock_connector.download_content.return_value = b"%PDF-1.7 Synthetic Cloud Document Content"
        mock_connector.fetch_item_permissions.return_value = [
            CloudPrincipalPermission("user", email="mapped@example.test")
        ]

        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
        mock_db.scalar = AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), acl_status="quarantined", acl_quarantine_reason="pending")
        )
        mock_audit_ctx = MagicMock()
        mock_settings = MagicMock(spec=Settings)
        mock_settings.data_dir = str(tmp_path)
        mock_settings.rag_max_upload_mb = 20

        conn_id = uuid4()
        import_observed = {}

        async def fake_import(_sessions, _settings, root, **kwargs):
            staged = list(root.glob("*.pdf"))
            assert len(staged) == 1
            assert staged[0].read_bytes().startswith(b"%PDF-")
            import_observed.update(kwargs)
            return {"queued": 1, "unchanged": 0, "failed": 0, "oversized": 0}

        from app.services.cloud_connectors import sync_engine

        monkeypatch.setattr(sync_engine, "import_folder", fake_import)
        monkeypatch.setattr(
            sync_engine, "_resolve_provider_permissions", AsyncMock(return_value=[("user", uuid4())])
        )
        result = await execute_connector_sync(
            connector=mock_connector,
            connector_id=conn_id,
            connector_name="Corporate OneDrive",
            visibility="restricted",
            department_group_id=uuid4(),
            cursor=None,
            settings=mock_settings,
            db=mock_db,
            audit_ctx=mock_audit_ctx,
            sessionmaker=MagicMock(),
            admin_email="admin@synthetic.invalid",
            target_dir=tmp_path / "staging",
        )

        assert result.status == "success"
        assert result.files_synced == 1
        assert result.files_queued == 1
        assert import_observed["source_system"] == "file_server"
        assert len(import_observed["source_uri_map"]) == 1
        assert result.next_cursor == "c_next_1"
        assert not list((tmp_path / "staging").glob("*.pdf"))

    asyncio.run(_run())


def test_cloud_sync_failure_details_are_redacted(tmp_path, caplog, monkeypatch):
    async def _run():
        mock_connector = AsyncMock()
        mock_connector.fetch_delta.return_value = DeltaSyncResult(
            items=[
                CloudItem(
                    id="synthetic_remote_id",
                    name="PRIVATE_DOCUMENT_NAME_SENTINEL.pdf",
                    mime_type="application/pdf",
                    size_bytes=1000,
                    is_deleted=False,
                )
            ],
            next_cursor="synthetic_cursor",
            deleted_ids=[],
        )
        mock_connector.download_content.side_effect = RuntimeError("ACCESS_TOKEN_SENTINEL")
        mock_connector.fetch_item_permissions.return_value = [
            CloudPrincipalPermission("user", email="mapped@example.test")
        ]
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_settings = MagicMock(spec=Settings)
        mock_settings.data_dir = str(tmp_path)
        mock_settings.rag_max_upload_mb = 20

        from app.services.cloud_connectors import sync_engine

        monkeypatch.setattr(
            sync_engine, "_resolve_provider_permissions", AsyncMock(return_value=[("user", uuid4())])
        )
        return await execute_connector_sync(
            connector=mock_connector,
            connector_id=uuid4(),
            connector_name="PRIVATE_CONNECTOR_NAME_SENTINEL",
            visibility="restricted",
            department_group_id=uuid4(),
            cursor=None,
            settings=mock_settings,
            db=mock_db,
            audit_ctx=MagicMock(),
            sessionmaker=MagicMock(),
            admin_email="admin@synthetic.invalid",
            target_dir=tmp_path / "staging",
        )

    caplog.set_level(logging.DEBUG, logger="app.services.cloud_connectors.sync_engine")
    result = asyncio.run(_run())

    assert result.status == "failed"
    assert result.errors == ["A cloud source file could not be downloaded."]
    for sentinel in (
        "ACCESS_TOKEN_SENTINEL",
        "PRIVATE_DOCUMENT_NAME_SENTINEL",
        "PRIVATE_CONNECTOR_NAME_SENTINEL",
    ):
        assert sentinel not in caplog.text


def test_build_connector_instance_factory():
    g_conn = build_connector_instance(
        "google_drive",
        {"client_id": "c1", "client_secret": "s1", "refresh_token": "r1"},
    )
    assert isinstance(g_conn, GoogleDriveConnector)

    ms_conn = build_connector_instance(
        "onedrive",
        {"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
    )
    assert isinstance(ms_conn, MicrosoftGraphConnector)


def test_cloud_delta_queues_restricted_document_worker_indexes_and_revokes_on_source_delete(client, monkeypatch, fake_provider):
    """Provider retry, real importer/worker, citation, ACL revocation, and source retention."""
    item = CloudItem(
        id="synthetic-cloud-item-42",
        name="cloud-runbook.txt",
        mime_type="text/plain",
        size_bytes=120,
        remote_acls=["member@example.com"],
    )
    content = b"Synthetic valve inspection must happen every 45 days. Use the blue gauge before restart.\n"

    class SyntheticConnector:
        delta = DeltaSyncResult(items=[item], next_cursor="synthetic-delta-1")
        fail_downloads = 1
        permissions = [CloudPrincipalPermission("user", email=USER2_EMAIL)]

        async def fetch_delta(self, cursor=None):
            return self.delta

        async def download_content(self, item_id):
            assert item_id == item.id
            if self.fail_downloads:
                self.fail_downloads -= 1
                raise RuntimeError("SYNTHETIC_PROVIDER_RETRY_SECRET")
            return content

        async def fetch_item_permissions(self, item_id):
            assert item_id == item.id
            return self.permissions

        def map_acls(self, remote_acls, fallback_visibility="restricted"):
            return {"visibility": "restricted", "allowed_emails": [v.lower() for v in remote_acls]}

        async def validate_connection(self):
            return {"status": "connected"}

    connector = SyntheticConnector()
    from app.api.routes import cloud_connectors as routes

    monkeypatch.setattr(routes, "build_connector_instance", lambda *_args, **_kwargs: connector)
    login(client, ADMIN_EMAIL)
    group_response = client.post(
        "/api/admin/access/groups", json={"name": "Cloud Import Engineering", "kind": "department"}
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["id"]
    assert client.post(
        f"/api/admin/access/groups/{group_id}/members", json={"email": USER2_EMAIL}
    ).status_code == 204
    group_members = client.get(f"/api/admin/access/groups/{group_id}/members").json()["members"]
    member_id = next(member["user_id"] for member in group_members if member["email"] == USER2_EMAIL)

    created = client.post(
        "/api/connectors",
        json={
            "name": "Synthetic Engineering Drive",
            "connector_type": "google_drive",
            "client_id": "synthetic-client-id",
            "client_secret": "SYNTHETIC_CLIENT_SECRET",
            "refresh_token": "SYNTHETIC_REFRESH_TOKEN",
            "visibility": "restricted",
            "department_group_id": group_id,
        },
    )
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    first_sync = client.post(f"/api/connectors/{connector_id}/sync")
    assert first_sync.status_code == 200, first_sync.text
    assert first_sync.json()["status"] == "failed"
    assert first_sync.json()["files_queued"] == 0

    async def read_connector_cursor():
        async with app.state.sessionmaker() as db:
            row = await db.get(CloudConnectorConfig, connector_id)
            return row.last_cursor

    assert client.portal.call(read_connector_cursor) is None
    retry = client.post(f"/api/connectors/{connector_id}/sync")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "success"
    assert (retry.json()["files_synced"], retry.json()["files_queued"], retry.json()["files_unchanged"]) == (1, 1, 0)
    assert "SYNTHETIC_PROVIDER_RETRY_SECRET" not in retry.text
    duplicate_sync = client.post(f"/api/connectors/{connector_id}/sync")
    assert duplicate_sync.status_code == 200
    assert (duplicate_sync.json()["files_queued"], duplicate_sync.json()["files_unchanged"]) == (0, 1)

    async def cloud_document():
        import hashlib

        uri = f"cloud://{connector_id}/{hashlib.sha256(item.id.encode()).hexdigest()}"
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(Document).where(Document.source_uri == uri))
            acl = list((await db.scalars(select(ResourceAcl).where(ResourceAcl.document_id == row.id))).all())
            provider_acl = list((await db.scalars(select(CloudSourceAcl).where(CloudSourceAcl.document_id == row.id))).all())
            return row, acl, provider_acl

    from sqlalchemy import select

    document_row, acl_rows, provider_acl_rows = client.portal.call(cloud_document)
    assert document_row.source_system == "file_server"
    assert document_row.visibility == "restricted" and document_row.acl_status == "active"
    assert len(acl_rows) == 1 and str(acl_rows[0].principal_group_id) == group_id
    assert len(provider_acl_rows) == 1 and str(provider_acl_rows[0].principal_user_id) == member_id
    assert document_row.status == "pending"

    worker = IngestionWorker(
        app.state.sessionmaker,
        get_settings(),
        app.state.rag.storage,
        app.state.rag.embedder,
        app.state.rag.indexer,
    )
    assert client.portal.call(worker.process_next) is True

    client.cookies.clear()
    login(client, USER2_EMAIL)
    ready = client.get(f"/api/rag/documents/{document_row.id}")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready" and ready.json()["searchable"] is True
    original = client.get(f"/api/rag/documents/{document_row.id}/file")
    assert original.status_code == 200
    assert original.headers["content-disposition"].endswith('filename="cloud-runbook.txt"')
    assert hashlib.sha256(original.content).hexdigest() == hashlib.sha256(content).hexdigest()

    fake_provider.rag_reply = lambda _message: "Inspect the valve every 45 days [S1]."
    activate_provider(client)
    client.cookies.clear()
    login(client, USER2_EMAIL)
    answer = ask(client, "How frequently is the synthetic valve inspected?")
    assert answer.status_code == 200, answer.text
    assert [citation["filename"] for citation in answer.json()["citations"]] == ["cloud-runbook.txt"]

    client.cookies.clear()
    login(client, USER_EMAIL)
    assert client.get(f"/api/rag/documents/{document_row.id}").status_code == 404
    assert client.get(f"/api/rag/documents/{document_row.id}/file").status_code == 404

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    assert client.delete(
        f"/api/admin/access/groups/{group_id}/members/{member_id}"
    ).status_code == 204
    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_row.id}").status_code == 404

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    assert client.post(
        f"/api/admin/access/groups/{group_id}/members", json={"email": USER2_EMAIL}
    ).status_code == 204

    connector.permissions = []
    connector.delta = DeltaSyncResult(items=[item], next_cursor="synthetic-delta-2")
    permission_revocation = client.post(f"/api/connectors/{connector_id}/sync")
    assert permission_revocation.status_code == 200
    assert permission_revocation.json()["status"] == "failed"

    async def provider_acl_count():
        async with app.state.sessionmaker() as db:
            return len((await db.scalars(select(CloudSourceAcl).where(CloudSourceAcl.document_id == document_row.id))).all())

    assert client.portal.call(provider_acl_count) == 0
    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_row.id}").status_code == 404

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    connector.delta = DeltaSyncResult(items=[], next_cursor="synthetic-delta-3", deleted_ids=[item.id])
    deleted = client.post(f"/api/connectors/{connector_id}/sync")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["files_deleted"] == 1
    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_row.id}").status_code == 404
    assert app.state.rag.storage.path_for(document_row.storage_key).read_bytes() == content

    async def retained_quarantine():
        async with app.state.sessionmaker() as db:
            row = await db.get(Document, document_row.id)
            provider_acl = list(
                (await db.scalars(select(CloudSourceAcl).where(CloudSourceAcl.document_id == row.id))).all()
            )
            return row.acl_status, row.acl_quarantine_reason, row.status, len(provider_acl)

    assert client.portal.call(retained_quarantine) == ("quarantined", "source_removed", "ready", 0)
