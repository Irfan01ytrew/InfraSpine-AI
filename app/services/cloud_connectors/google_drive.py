"""Google Drive cloud storage connector with delta synchronization and ACL mapping."""

from __future__ import annotations

import logging
import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import InvalidInputError
from app.services.cloud_connectors.base import (
    BaseCloudConnector,
    CloudItem,
    CloudPrincipalPermission,
    DeltaSyncResult,
    PermissionReconciliationPage,
)

logger = logging.getLogger(__name__)

# Google Workspace export MIME mappings for proprietary office files
_GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_MAX_PERMISSION_PAGES = 10
_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
_RECONCILE_CURSOR_PREFIX = "gdrive-folder-reconcile:v1:"
_MAX_RECONCILE_DEPTH = 32
_MAX_RECONCILE_CURSOR_BYTES = 256_000


def _encode_folder_reconciliation_cursor(root_id: str, stack: list[dict[str, Any]]) -> str:
    payload = json.dumps({"root": root_id, "stack": stack}, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_RECONCILE_CURSOR_BYTES:
        raise InvalidInputError("Google Drive folder reconciliation state exceeded its safe bound.")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return _RECONCILE_CURSOR_PREFIX + token


def _decode_folder_reconciliation_cursor(cursor: str, root_id: str) -> list[dict[str, Any]]:
    if not cursor.startswith(_RECONCILE_CURSOR_PREFIX):
        # Migration from the earlier direct-children cursor: it was the root's Drive page token.
        return [{"id": root_id, "page_token": cursor, "started": True, "exhausted": False, "children": []}]
    encoded = cursor[len(_RECONCILE_CURSOR_PREFIX):]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) > _MAX_RECONCILE_CURSOR_BYTES:
            raise ValueError
        state = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        raise InvalidInputError("Google Drive folder reconciliation cursor is invalid.") from None
    if not isinstance(state, dict) or state.get("root") != root_id or not isinstance(state.get("stack"), list):
        raise InvalidInputError("Google Drive folder reconciliation cursor no longer matches its configured folder.")
    stack = state["stack"]
    if len(stack) > _MAX_RECONCILE_DEPTH:
        raise InvalidInputError("Google Drive folder reconciliation depth exceeded its safe bound.")
    for frame in stack:
        if (
            not isinstance(frame, dict)
            or not isinstance(frame.get("id"), str)
            or not frame["id"]
            or not isinstance(frame.get("started"), bool)
            or not isinstance(frame.get("exhausted"), bool)
            or not isinstance(frame.get("children"), list)
            or len(frame["children"]) > 100
            or any(not isinstance(child, str) or not child for child in frame["children"])
            or (frame.get("page_token") is not None and not isinstance(frame.get("page_token"), str))
        ):
            raise InvalidInputError("Google Drive folder reconciliation cursor is invalid.")
    return stack


class GoogleDriveConnector(BaseCloudConnector):
    """Google Drive API v3 connector supporting shared drives and delta tokens."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_id: str | None = None,
        drive_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.folder_id = folder_id.strip() if folder_id else None
        self.drive_id = drive_id.strip() if drive_id else None
        self._access_token: str | None = None
        self._custom_client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._custom_client or httpx.AsyncClient(timeout=30.0)

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        """Exchange refresh token for an ephemeral access token."""
        if self._access_token:
            return self._access_token
        try:
            resp = await client.post(
                _OAUTH_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code != 200:
                logger.error("Google Drive OAuth exchange failed: HTTP %s", resp.status_code)
                raise InvalidInputError("Google Drive authentication failed; check OAuth client credentials.")
            payload = resp.json()
            token = payload.get("access_token")
            if not token:
                raise InvalidInputError("Google Drive OAuth response contained no access token.")
            self._access_token = token
            return token
        except httpx.RequestError as exc:
            logger.error("Google Drive token request transport error_type=%s", type(exc).__name__)
            raise InvalidInputError("Cannot reach Google OAuth endpoint.") from exc

    async def validate_connection(self) -> dict[str, Any]:
        """Validate connectivity and return drive metadata."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = f"{_DRIVE_API_BASE}/about"
        params = {"fields": "user,storageQuota"}
        resp = await client.get(endpoint, headers=headers, params=params)
        if resp.status_code != 200:
            raise InvalidInputError(f"Google Drive verification failed with HTTP {resp.status_code}.")
        data = resp.json()
        user_info = data.get("user", {})
        return {
            "status": "connected",
            "account_email": user_info.get("emailAddress", "unknown"),
            "display_name": user_info.get("displayName", "Google Drive User"),
        }

    async def fetch_delta(self, cursor: str | None = None, page_size: int = 100) -> DeltaSyncResult:
        """Fetch changes using Google Drive Changes API."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}

        # If no cursor provided, fetch startPageToken first
        if not cursor:
            resp = await client.get(
                f"{_DRIVE_API_BASE}/changes/startPageToken",
                headers=headers,
                params={"supportsAllDrives": "true"},
            )
            if resp.status_code != 200:
                raise InvalidInputError("Failed to initialize Google Drive change token.")
            cursor = resp.json().get("startPageToken")

        params: dict[str, Any] = {
            "pageToken": cursor,
            "pageSize": min(page_size, 1000),
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,size,modifiedTime,permissions(emailAddress,role)))",
        }
        if self.drive_id:
            params["driveId"] = self.drive_id

        resp = await client.get(f"{_DRIVE_API_BASE}/changes", headers=headers, params=params)
        if resp.status_code != 200:
            raise InvalidInputError(f"Failed to query Google Drive changes: HTTP {resp.status_code}")

        data = resp.json()
        items: list[CloudItem] = []
        deleted_ids: list[str] = []

        for ch in data.get("changes", []):
            file_id = ch.get("fileId")
            if ch.get("removed"):
                deleted_ids.append(file_id)
                items.append(CloudItem(id=file_id, name="", mime_type="", is_deleted=True))
                continue

            file_info = ch.get("file")
            if not file_info:
                continue

            mime_type = file_info.get("mimeType", "")
            is_folder = mime_type == "application/vnd.google-apps.folder"

            # Parse permissions for ACL mapping
            raw_perms = file_info.get("permissions", [])
            acls = [p.get("emailAddress") for p in raw_perms if p.get("emailAddress")]

            mod_str = file_info.get("modifiedTime")
            mod_dt = None
            if mod_str:
                try:
                    mod_dt = datetime.fromisoformat(mod_str.replace("Z", "+00:00"))
                except ValueError:
                    mod_dt = datetime.now(UTC)

            items.append(
                CloudItem(
                    id=file_info.get("id", file_id),
                    name=file_info.get("name", "untitled"),
                    mime_type=mime_type,
                    size_bytes=int(file_info.get("size", 0)),
                    is_folder=is_folder,
                    is_deleted=False,
                    modified_at=mod_dt,
                    remote_acls=acls,
                )
            )

        next_page = data.get("nextPageToken")
        new_start = data.get("newStartPageToken")
        next_cursor = next_page or new_start

        return DeltaSyncResult(
            items=items,
            next_cursor=next_cursor,
            has_more=bool(next_page),
            deleted_ids=deleted_ids,
        )

    async def fetch_permission_reconciliation_page(
        self, cursor: str | None = None, page_size: int = 50
    ) -> PermissionReconciliationPage:
        """Enumerate a bounded Drive inventory page, recursively for a selected folder.

        Folder traversal uses a private depth-first cursor. Its depth and serialized size are bounded;
        missing items in any page never mean that a file was deleted or should lose access.
        """
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}
        bounded_page_size = min(max(page_size, 1), 100)
        stack: list[dict[str, Any]] | None = None
        if self.folder_id:
            stack = (
                _decode_folder_reconciliation_cursor(cursor, self.folder_id)
                if cursor
                else [{"id": self.folder_id, "page_token": None, "started": False, "exhausted": False, "children": []}]
            )
            while stack:
                frame = stack[-1]
                if frame["children"]:
                    child_id = frame["children"].pop()
                    if child_id in {entry["id"] for entry in stack}:
                        continue
                    if len(stack) >= _MAX_RECONCILE_DEPTH:
                        raise InvalidInputError("Google Drive folder nesting exceeded the safe reconciliation depth.")
                    stack.append(
                        {"id": child_id, "page_token": None, "started": False, "exhausted": False, "children": []}
                    )
                    continue
                if frame["exhausted"]:
                    stack.pop()
                    continue
                break
            if not stack:
                return PermissionReconciliationPage(items=[], next_cursor=None, complete=True)

        params: dict[str, Any] = {
            "pageSize": bounded_page_size,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "q": "trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
        }
        page_token = cursor
        if stack is not None:
            frame = stack[-1]
            page_token = frame["page_token"] if frame["started"] else None
            escaped_folder = frame["id"].replace("\\", "\\\\").replace("'", "\\'")
            params["q"] += f" and '{escaped_folder}' in parents"
        if page_token:
            params["pageToken"] = page_token
        if self.drive_id:
            params["driveId"] = self.drive_id
            params["corpora"] = "drive"

        response = await client.get(f"{_DRIVE_API_BASE}/files", headers=headers, params=params)
        if response.status_code != 200:
            logger.warning("Google Drive reconciliation inventory failed: HTTP %s", response.status_code)
            raise InvalidInputError("Google Drive library permissions could not be reconciled.")
        data = response.json()
        provider_page_token = data.get("nextPageToken")
        if provider_page_token and provider_page_token == page_token:
            raise InvalidInputError("Google Drive library reconciliation did not advance safely.")

        items: list[CloudItem] = []
        for file_info in data.get("files", []):
            item_id = file_info.get("id")
            if not item_id:
                continue
            modified_at = None
            mod_str = file_info.get("modifiedTime")
            if mod_str:
                try:
                    modified_at = datetime.fromisoformat(mod_str.replace("Z", "+00:00"))
                except ValueError:
                    modified_at = None
            try:
                size_bytes = int(file_info.get("size", 0))
            except (TypeError, ValueError):
                size_bytes = 0
            mime_type = file_info.get("mimeType", "")
            items.append(
                CloudItem(
                    id=item_id,
                    name=file_info.get("name", "untitled"),
                    mime_type=mime_type,
                    size_bytes=max(size_bytes, 0),
                    modified_at=modified_at,
                    is_folder=mime_type == _GOOGLE_FOLDER_MIME,
                )
            )
        if stack is None:
            return PermissionReconciliationPage(
                items=items,
                next_cursor=provider_page_token,
                complete=not bool(provider_page_token),
            )

        frame = stack[-1]
        ancestors = {entry["id"] for entry in stack}
        children = list(
            dict.fromkeys(item.id for item in items if item.is_folder and item.id not in ancestors)
        )
        frame["children"] = list(reversed(children))
        frame["started"] = True
        frame["page_token"] = provider_page_token
        frame["exhausted"] = not bool(provider_page_token)
        while stack and stack[-1]["exhausted"] and not stack[-1]["children"]:
            stack.pop()
        complete = not stack
        next_cursor = None if complete else _encode_folder_reconciliation_cursor(self.folder_id, stack)
        return PermissionReconciliationPage(
            items=items,
            next_cursor=next_cursor,
            complete=complete,
        )

    async def download_content(self, item_id: str) -> bytes:
        """Download binary content or export Google Workspace documents."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Check file metadata for Google Workspace format
        meta_resp = await client.get(
            f"{_DRIVE_API_BASE}/files/{item_id}",
            headers=headers,
            params={"fields": "mimeType,name", "supportsAllDrives": "true"},
        )
        if meta_resp.status_code != 200:
            raise InvalidInputError(f"Could not retrieve Google Drive file metadata: HTTP {meta_resp.status_code}")

        mime_type = meta_resp.json().get("mimeType", "")

        # Export Google Docs/Sheets/Slides if needed
        if mime_type in _GOOGLE_DOC_EXPORTS:
            export_mime, _ = _GOOGLE_DOC_EXPORTS[mime_type]
            url = f"{_DRIVE_API_BASE}/files/{item_id}/export"
            dl_resp = await client.get(url, headers=headers, params={"mimeType": export_mime})
        else:
            url = f"{_DRIVE_API_BASE}/files/{item_id}"
            dl_resp = await client.get(url, headers=headers, params={"alt": "media", "supportsAllDrives": "true"})

        if dl_resp.status_code != 200:
            raise InvalidInputError(f"Failed to download Google Drive item: HTTP {dl_resp.status_code}")

        return dl_resp.content

    async def fetch_item_permissions(self, item_id: str) -> list[CloudPrincipalPermission]:
        """Fetch every bounded live permission page, including provider inheritance details."""
        client = await self._get_client()
        token = await self._authenticate(client)
        url = f"{_DRIVE_API_BASE}/files/{item_id}/permissions"
        headers = {"Authorization": f"Bearer {token}"}
        params: dict[str, Any] | None = {
            "supportsAllDrives": "true",
            "pageSize": 100,
            "fields": (
                "nextPageToken,permissions(id,type,role,emailAddress,deleted,"
                "permissionDetails(inherited,inheritedFrom,permissionType,role))"
            ),
        }
        permissions: list[CloudPrincipalPermission] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _page in range(_MAX_PERMISSION_PAGES):
            if page_token is not None:
                if not page_token or page_token in seen_tokens:
                    raise InvalidInputError("Google Drive item permissions could not be verified.")
                seen_tokens.add(page_token)
                params = {**(params or {}), "pageToken": page_token}
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                logger.warning("Google Drive permission refresh failed: HTTP %s", resp.status_code)
                raise InvalidInputError("Google Drive item permissions could not be verified.")

            data = resp.json()
            for permission in data.get("permissions", []):
                if permission.get("deleted"):
                    continue
                role = permission.get("role")
                rights = {
                    "reader": "read",
                    "commenter": "read",
                    "writer": "modify",
                    "fileOrganizer": "modify",
                    "organizer": "modify",
                    "owner": "full_control",
                }.get(role, "unsupported")
                permissions.append(
                    CloudPrincipalPermission(
                        principal_type=permission.get("type", "unknown"),
                        external_id=permission.get("id"),
                        email=permission.get("emailAddress"),
                        rights=rights,
                    )
                )
                if len(permissions) > 1000:
                    raise InvalidInputError("Google Drive item has too many permissions to verify safely.")

            page_token = data.get("nextPageToken")
            if not page_token:
                return permissions
        raise InvalidInputError("Google Drive item permission pagination exceeded its safety limit.")

    def map_acls(self, remote_acls: list[str], fallback_visibility: str = "restricted") -> dict[str, Any]:
        """Convert Google Drive permissions into InfraSpine visibility and allowed principals."""
        valid_emails = [e.strip().lower() for e in remote_acls if "@" in e]
        if not valid_emails:
            return {
                "visibility": fallback_visibility,
                "allowed_emails": [],
            }
        return {
            "visibility": "restricted",
            "allowed_emails": valid_emails,
        }
