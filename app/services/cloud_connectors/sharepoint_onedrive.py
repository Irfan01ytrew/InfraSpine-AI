"""Microsoft Graph cloud storage connector for OneDrive and SharePoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

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

_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"
_MAX_PERMISSION_PAGES = 10


def _trusted_graph_page_url(url: str, expected_path: str) -> str:
    """Accept provider pagination URLs only for the exact Graph endpoint in use."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise InvalidInputError("Microsoft Graph returned an invalid pagination URL.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or not parsed.query
        or parsed.fragment
    ):
        raise InvalidInputError("Microsoft Graph returned an untrusted pagination URL.")
    return url


class MicrosoftGraphConnector(BaseCloudConnector):
    """Microsoft Graph connector supporting OneDrive for Business and SharePoint Online libraries."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        drive_id: str | None = None,
        site_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.drive_id = drive_id.strip() if drive_id else None
        self.site_id = site_id.strip() if site_id else None
        self._access_token: str | None = None
        self._custom_client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._custom_client or httpx.AsyncClient(timeout=30.0)

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        """Acquire an app-only application token using Azure AD OAuth client credentials."""
        if self._access_token:
            return self._access_token
        url = f"{_LOGIN_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        try:
            resp = await client.post(
                url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
            if resp.status_code != 200:
                logger.error("Microsoft Azure AD OAuth token request failed: HTTP %s", resp.status_code)
                raise InvalidInputError("Microsoft 365 OAuth authentication failed; check tenant and client credentials.")
            payload = resp.json()
            token = payload.get("access_token")
            if not token:
                raise InvalidInputError("Azure AD OAuth response contained no access token.")
            self._access_token = token
            return token
        except httpx.RequestError as exc:
            logger.error("Azure AD token request network error_type=%s", type(exc).__name__)
            raise InvalidInputError("Cannot reach Azure Active Directory login service.") from exc

    async def _resolve_drive_endpoint(self, client: httpx.AsyncClient, headers: dict[str, str]) -> str:
        """Determine base drive endpoint (site drive or default tenant drive)."""
        if self.drive_id:
            return f"{_GRAPH_API_BASE}/drives/{self.drive_id}"
        if self.site_id:
            resp = await client.get(f"{_GRAPH_API_BASE}/sites/{self.site_id}/drive", headers=headers)
            if resp.status_code == 200:
                d_id = resp.json().get("id")
                if d_id:
                    self.drive_id = d_id
                    return f"{_GRAPH_API_BASE}/drives/{d_id}"
        # Fall back to root drives endpoint
        return f"{_GRAPH_API_BASE}/drive"

    async def validate_connection(self) -> dict[str, Any]:
        """Verify token and retrieve drive/tenant info."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}
        drive_url = await self._resolve_drive_endpoint(client, headers)
        resp = await client.get(drive_url, headers=headers)
        if resp.status_code != 200:
            raise InvalidInputError(f"Microsoft Graph drive lookup failed: HTTP {resp.status_code}")
        data = resp.json()
        return {
            "status": "connected",
            "drive_id": data.get("id", "root"),
            "drive_name": data.get("name", "Document Library"),
            "drive_type": data.get("driveType", "business"),
        }

    async def fetch_delta(self, cursor: str | None = None, page_size: int = 100) -> DeltaSyncResult:
        """Fetch incremental changes using Microsoft Graph delta token."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}", "Prefer": f"odata.maxpagesize={min(page_size, 500)}"}
        drive_url = await self._resolve_drive_endpoint(client, headers)

        delta_path = f"{urlsplit(drive_url).path}/root/delta"
        if cursor and "://" in cursor:
            url = _trusted_graph_page_url(cursor, delta_path)
        elif cursor:
            url = f"{drive_url}/root/delta?token={cursor}"
        else:
            url = f"{drive_url}/root/delta"

        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise InvalidInputError(f"Microsoft Graph delta sync failed with HTTP {resp.status_code}")

        data = resp.json()
        items: list[CloudItem] = []
        deleted_ids: list[str] = []

        for row in data.get("value", []):
            item_id = row.get("id")
            if not item_id:
                continue

            # Deleted items carry the '@removed' or 'deleted' facet
            if "@removed" in row or "deleted" in row:
                deleted_ids.append(item_id)
                items.append(CloudItem(id=item_id, name="", mime_type="", is_deleted=True))
                continue

            is_folder = "folder" in row
            file_meta = row.get("file", {})
            mime_type = file_meta.get("mimeType", "application/octet-stream" if not is_folder else "")

            mod_str = row.get("lastModifiedDateTime")
            mod_dt = None
            if mod_str:
                try:
                    mod_dt = datetime.fromisoformat(mod_str.replace("Z", "+00:00"))
                except ValueError:
                    mod_dt = datetime.now(UTC)

            # Extract permission roles / identities if present
            raw_perms = row.get("permissions", [])
            acls = []
            for perm in raw_perms:
                granted = perm.get("grantedToV2") or perm.get("grantedTo") or {}
                user_obj = granted.get("user") or granted.get("group") or {}
                email = user_obj.get("email") or user_obj.get("userPrincipalName")
                if email:
                    acls.append(email.lower())

            items.append(
                CloudItem(
                    id=item_id,
                    name=row.get("name", "untitled"),
                    mime_type=mime_type,
                    size_bytes=int(row.get("size", 0)),
                    is_folder=is_folder,
                    is_deleted=False,
                    modified_at=mod_dt,
                    remote_acls=acls,
                    download_url=row.get("@microsoft.graph.downloadUrl"),
                )
            )

        next_link = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink")
        if next_link:
            next_link = _trusted_graph_page_url(next_link, delta_path)
        if delta_link:
            delta_link = _trusted_graph_page_url(delta_link, delta_path)
        next_cursor = next_link or delta_link

        return DeltaSyncResult(
            items=items,
            next_cursor=next_cursor,
            has_more=bool(next_link),
            deleted_ids=deleted_ids,
        )

    async def fetch_permission_reconciliation_page(
        self, cursor: str | None = None, page_size: int = 50
    ) -> PermissionReconciliationPage:
        """Use Graph's independently paged initial delta inventory for one bounded ACL pass."""
        page = await self.fetch_delta(cursor=cursor, page_size=min(max(page_size, 1), 100))
        # Delta tombstones are deliberately not exposed as deletion evidence here. This scan
        # only refreshes ACLs for items it sees; files absent from a page are left untouched.
        return PermissionReconciliationPage(
            items=[item for item in page.items if not item.is_deleted],
            next_cursor=page.next_cursor if page.has_more else None,
            complete=not page.has_more,
        )

    async def download_content(self, item_id: str) -> bytes:
        """Download binary content using /content endpoint."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}
        drive_url = await self._resolve_drive_endpoint(client, headers)
        content_url = f"{drive_url}/items/{item_id}/content"

        # Microsoft Graph redirects to pre-authenticated storage URL
        resp = await client.get(content_url, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            raise InvalidInputError(f"Failed to download OneDrive/SharePoint content: HTTP {resp.status_code}")
        return resp.content

    async def fetch_item_permissions(self, item_id: str) -> list[CloudPrincipalPermission]:
        """Read every bounded Graph permission page; delta payloads are not authoritative."""
        client = await self._get_client()
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}"}
        drive_url = await self._resolve_drive_endpoint(client, headers)
        permission_url = f"{drive_url}/items/{item_id}/permissions"
        expected_path = urlsplit(permission_url).path
        url: str | None = permission_url
        params: dict[str, Any] | None = {
            "$top": 100,
            "$select": "id,roles,grantedToV2,grantedToIdentitiesV2,link,inheritedFrom",
        }
        permissions: list[CloudPrincipalPermission] = []
        seen_urls: set[str] = set()
        for _page in range(_MAX_PERMISSION_PAGES):
            if url is None or url in seen_urls:
                raise InvalidInputError("Microsoft 365 item permissions could not be verified.")
            seen_urls.add(url)
            if url == permission_url:
                resp = await client.get(url, headers=headers, params=params)
            else:
                url = _trusted_graph_page_url(url, expected_path)
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("Microsoft Graph permission refresh failed: HTTP %s", resp.status_code)
                raise InvalidInputError("Microsoft 365 item permissions could not be verified.")

            data = resp.json()
            for permission in data.get("value", []):
                roles = permission.get("roles") or []
                if "read" in roles:
                    rights = "read"
                elif set(roles) & {"write", "owner"}:
                    rights = "modify" if "write" in roles else "full_control"
                else:
                    rights = "unsupported"

                principals = permission.get("grantedToIdentitiesV2")
                if not principals:
                    principals = [permission.get("grantedToV2") or permission.get("grantedTo") or {}]
                for granted in principals:
                    if "link" in permission:
                        permissions.append(
                            CloudPrincipalPermission(principal_type="link", rights=rights)
                        )
                        continue
                    user = granted.get("user") or {}
                    group = granted.get("group") or {}
                    site_group = granted.get("siteGroup") or {}
                    if user:
                        permissions.append(
                            CloudPrincipalPermission(
                                principal_type="user",
                                external_id=user.get("id"),
                                email=user.get("email") or user.get("userPrincipalName"),
                                rights=rights,
                            )
                        )
                    elif group or site_group:
                        subject = group or site_group
                        permissions.append(
                            CloudPrincipalPermission(
                                principal_type="group",
                                external_id=subject.get("id"),
                                rights=rights,
                            )
                        )
                    else:
                        permissions.append(
                            CloudPrincipalPermission(principal_type="unknown", rights=rights)
                        )
                if len(permissions) > 1000:
                    raise InvalidInputError("Microsoft 365 item has too many permissions to verify safely.")

            next_link = data.get("@odata.nextLink")
            if not next_link:
                return permissions
            url = _trusted_graph_page_url(next_link, expected_path)
            params = None
        raise InvalidInputError("Microsoft 365 item permission pagination exceeded its safety limit.")

    def map_acls(self, remote_acls: list[str], fallback_visibility: str = "restricted") -> dict[str, Any]:
        """Convert Microsoft permissions into InfraSpine visibility and allowed principals."""
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
