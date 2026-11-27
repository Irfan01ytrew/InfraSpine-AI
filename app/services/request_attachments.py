"""Files attached to requests through a form's file field.

A file is uploaded first (owned by the uploader, not yet on a request), then linked when the request is
submitted. Files live in private storage under random names (`DocumentStorage`); the client's filename is
display metadata only. Accepted: PDF, Word, Excel, PowerPoint, text, CSV, PNG and JPEG up to 10 MB, checked
against their content signature. Anyone who can read the request can download its files; uploads never
attached to a request are removed after a day.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import FileRejectedError, FileTooLargeError, InvalidInputError, NotFoundError
from app.db.form_models import RequestAttachment
from app.db.models import User
from app.rag.storage import DocumentStorage, UnsafeFileError, sanitize_filename

logger = logging.getLogger(__name__)
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_FIELD = 5
MAX_PENDING_PER_USER = 20
ORPHAN_AFTER = timedelta(hours=24)
CLEANUP_POLL_SECONDS = 3600
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain", ".csv": "text/csv", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}
_SIGNATURES = {".pdf": (b"%PDF-",), ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
               ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",), ".pptx": (b"PK\x03\x04",)}


def storage(settings: Settings | None = None) -> DocumentStorage:
    return DocumentStorage((settings or get_settings()).rag_data_dir.resolve() / "request_attachments")


def _content_matches(path: Path, extension: str) -> bool:
    with path.open("rb") as handle:
        head = handle.read(8192)
    if extension in _SIGNATURES:
        return head.startswith(_SIGNATURES[extension])
    try:  # text and CSV: UTF-8 without NUL bytes (a character cut at the end of the sample is fine)
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return b"\x00" not in head


async def upload(db: AsyncSession, user: User, filename: str | None, source, settings: Settings | None = None) -> RequestAttachment:
    try:
        safe_name = sanitize_filename(filename, tuple(CONTENT_TYPES))
    except UnsafeFileError as exc:
        raise FileRejectedError("Attach a PDF, Word, Excel, PowerPoint, text, CSV, PNG or JPEG file.") from exc
    pending = await db.scalar(select(func.count()).select_from(RequestAttachment).where(
        RequestAttachment.uploader_user_id == user.id, RequestAttachment.request_id.is_(None)))
    if pending >= MAX_PENDING_PER_USER:
        raise InvalidInputError("Submit or remove the files you already attached before adding more.")
    extension = PurePath(safe_name).suffix.lower()
    files = storage(settings)
    try:
        stored = await asyncio.to_thread(files.save, source, MAX_BYTES, extension)
    except UnsafeFileError as exc:
        if exc.code == "file_too_large":
            raise FileTooLargeError("Attachments are limited to 10 MB each.") from None
        raise FileRejectedError(exc.message) from None
    if not await asyncio.to_thread(_content_matches, files.path_for(stored.key), extension):
        await asyncio.to_thread(files.delete, stored.key)
        raise FileRejectedError("The file's content does not match its type.")
    row = RequestAttachment(uploader_user_id=user.id, filename=safe_name, content_type=CONTENT_TYPES[extension],
                            size_bytes=stored.size_bytes, sha256=stored.sha256, storage_key=stored.key)
    db.add(row)
    await db.commit()
    return row


async def remove_pending(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> None:
    row = await db.get(RequestAttachment, attachment_id, with_for_update=True)
    if row is None or row.uploader_user_id != user.id or row.request_id is not None:
        raise NotFoundError("Attachment not found.")
    key = row.storage_key
    await db.delete(row)
    await db.commit()
    await asyncio.to_thread(storage().delete, key)


async def claim(db: AsyncSession, user: User, ids: list[str]) -> list[RequestAttachment]:
    """Lock the user's own pending uploads for a request being submitted; link them with `link`."""
    if len(ids) > MAX_FILES_PER_FIELD:
        raise InvalidInputError(f"Attach at most {MAX_FILES_PER_FIELD} files per field.")
    try:
        wanted = [uuid.UUID(str(i)) for i in ids]
    except ValueError:
        raise InvalidInputError("An attachment is not valid; upload it again.") from None
    rows = (await db.scalars(select(RequestAttachment).where(RequestAttachment.id.in_(wanted)).with_for_update())).all()
    by_id = {row.id: row for row in rows}
    out = []
    for attachment_id in wanted:
        row = by_id.get(attachment_id)
        if row is None or row.uploader_user_id != user.id or row.request_id is not None:
            raise InvalidInputError("An attachment is no longer available; upload it again.")
        out.append(row)
    if len(set(wanted)) != len(wanted):
        raise InvalidInputError("The same file is attached twice.")
    return out


def link(rows: list[RequestAttachment], request_id: uuid.UUID, field_key: str) -> None:
    now = datetime.now(UTC)
    for row in rows:
        row.request_id, row.field_key, row.linked_at = request_id, field_key, now


async def cleanup_orphans(sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None, settings: Settings | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - ORPHAN_AFTER
    async with sessionmaker() as db:
        rows = (await db.scalars(select(RequestAttachment).where(
            RequestAttachment.request_id.is_(None), RequestAttachment.created_at < cutoff).limit(500).with_for_update(skip_locked=True))).all()
        keys = [row.storage_key for row in rows]
        for row in rows:
            await db.delete(row)
        await db.commit()
    files = storage(settings)
    for key in keys:
        await asyncio.to_thread(files.delete, key)
    return len(keys)


async def run_attachment_cleanup(sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *, interval_seconds: float = CLEANUP_POLL_SECONDS) -> None:
    while not stop.is_set():
        try:
            await cleanup_orphans(sessionmaker)
        except Exception as exc:
            logger.warning("Attachment cleanup failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
