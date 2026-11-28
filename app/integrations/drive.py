"""Google Drive connector class for Step V wider integrations (mock-transport based).

Thin wrapper over the Drive v3 API surface already used by
``app.services.cloud_connectors.google_drive`` (``/about`` / ``/files``),
exposing the generic registry interface: ``verify``, ``sync`` (list files),
``fetch_file`` (metadata) and ``download_file`` (media). It runs on an
injected transport, so tests and this slice never touch the real Drive API.
Read-only: Drive has no write operation in the catalog, so nothing here can
change remote files. Inbound events are normalised with
``registry.normalize_inbound_event`` and are always untrusted. The OAuth
client / refresh token belongs to the owner and is never stored here; the
OAuth-backed ``GoogleDriveConnector`` in ``cloud_connectors`` remains the
sync-engine implementation, and a later slice can adapt this class to
delegate to it.
"""

from __future__ import annotations

import re

from app.core.errors import InvalidInputError
from app.integrations import registry

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

# Drive file fields this connector ever returns. Everything else in a
# provider response is dropped: Drive content is untrusted data.
_FILE_ALLOWLIST = ("id", "name", "mimeType", "size", "modifiedTime")

_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")
_QUERY_PATTERN = re.compile(r"^[A-Za-z0-9 _='.\-]{1,500}$")


class DriveAuthError(InvalidInputError):
    """Raised when Drive rejects the credentials (401)."""


class DriveRateLimitedError(InvalidInputError):
    """Raised when Drive reports quota exhaustion / rate limiting (429)."""


def _error_from(result: object):
    """Map a Drive error envelope to a typed exception, or None."""
    if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
        return None
    error = result["error"]
    code = error.get("code")
    message = str(error.get("message", "")).lower()
    if code == 429 or "quota" in message or "ratelimitexceeded" in message:
        return DriveRateLimitedError("Drive quota/rate limit exceeded.")
    if code == 401 or "invalid_grant" in message or "unauthenticated" in message:
        return DriveAuthError("Drive credentials rejected.")
    return InvalidInputError("Drive request failed.")


def _file_record(row: object) -> dict:
    """Reduce one Drive file row to the allow-listed fields."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise InvalidInputError("Drive file record has no id.")
    return {field: row[field] for field in _FILE_ALLOWLIST if field in row}


class DriveConnector:
    """Drive ``verify`` + ``sync`` + file fetch, read-only."""

    provider = "google_drive"

    def __init__(self, transport, *, token: str = "mock-token"):
        self.transport = transport
        self.token = token

    async def verify(self) -> dict:
        """Call ``/about``; returns the Drive account email + display name."""
        result = await self.transport.get("/about")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("user"), dict):
            raise InvalidInputError("Drive verification failed.")
        user = result["user"]
        if not user.get("emailAddress"):
            raise InvalidInputError("Drive verification failed.")
        return {
            "ok": True,
            "emailAddress": user.get("emailAddress"),
            "displayName": user.get("displayName"),
        }

    async def list_files(
        self, *, page_size: int = 10, page_token: str | None = None, query: str | None = None
    ) -> dict:
        """List files (one page). ``query`` is a Drive search string."""
        if not 1 <= page_size <= 100:
            raise InvalidInputError("page_size must be between 1 and 100.")
        if page_token is not None and (
            not isinstance(page_token, str) or not page_token or len(page_token) > 2048
        ):
            raise InvalidInputError("Invalid Drive page token.")
        if query is not None and not _QUERY_PATTERN.fullmatch(query):
            raise InvalidInputError("Drive search query is invalid.")
        path = f"/files?pageSize={page_size}"
        if page_token:
            path += f"&pageToken={page_token}"
        if query:
            path += f"&q={query}"
        result = await self.transport.get(path)
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise InvalidInputError("Drive list failed.")
        return {
            "files": [_file_record(row) for row in result["files"]],
            "nextPageToken": result.get("nextPageToken"),
        }

    async def sync(self, *, page_token: str | None = None) -> dict:
        """One sync page: delegates to ``list_files`` (trashed files excluded)."""
        return await self.list_files(page_size=100, page_token=page_token, query="trashed = false")

    async def fetch_file(self, file_id: str) -> dict:
        """Fetch one file's metadata, allow-listed fields only."""
        if not isinstance(file_id, str) or not _FILE_ID_PATTERN.fullmatch(file_id):
            raise InvalidInputError("Invalid Drive file id.")
        result = await self.transport.get(
            f"/files/{file_id}?fields=id,name,mimeType,size,modifiedTime"
        )
        error = _error_from(result)
        if error is not None:
            raise error
        return _file_record(result)

    async def download_file(self, file_id: str) -> dict:
        """Download one file's media content (returned as text/bytes)."""
        if not isinstance(file_id, str) or not _FILE_ID_PATTERN.fullmatch(file_id):
            raise InvalidInputError("Invalid Drive file id.")
        result = await self.transport.get(f"/files/{file_id}?alt=media")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, (str, bytes)):
            raise InvalidInputError("Drive download failed.")
        return {"ok": True, "id": file_id, "content": result}

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound Drive change event. Always untrusted."""
        return registry.normalize_inbound_event("google_drive", raw)

    @staticmethod
    def base_url() -> str:
        return _DRIVE_BASE
