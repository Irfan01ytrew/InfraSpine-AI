"""Deliver only owner-bound InfraSpine exports and images through the official Cloud API."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import WhatsAppArtifactDispatch
from app.integrations.transport import TransportError, request as provider_request
from app.services import agent_studio_whatsapp as legacy
from app.services.chat_exports import MEDIA_TYPES as EXPORT_MEDIA_TYPES, MAX_EXPORT_BYTES, resolve_chat_export
from app.services.chat_images import resolve_chat_image

MAX_IMAGE_WHATSAPP_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_WHATSAPP_BYTES = MAX_EXPORT_BYTES
MAX_ARTIFACTS_PER_MESSAGE = 5
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]{0,300}\]\((/api/(?:exports/download/[^\s)]+|chat-images/[^\s)]+))\)")


@dataclass(frozen=True)
class ArtifactRef:
    source_path: str
    token: str
    kind: str
    file_format: str | None
    key: str


@dataclass(frozen=True)
class ArtifactFile:
    filename: str
    media_type: str
    body: bytes


def artifact_refs(answer: str) -> list[ArtifactRef]:
    """Extract local download links only; arbitrary model-provided URLs are never fetched."""
    found: list[ArtifactRef] = []
    seen: set[str] = set()
    for match in _MARKDOWN_LINK.finditer(answer):
        local_url = match.group(1)
        parsed = urlsplit(local_url)
        segments = parsed.path.split("/")
        if parsed.path.startswith("/api/exports/download/") and len(segments) == 5:
            token = segments[4]
            try:
                formats = parse_qs(parsed.query, strict_parsing=True).get("format", [])
            except ValueError:
                continue
            if len(formats) != 1 or formats[0] not in EXPORT_MEDIA_TYPES:
                continue
            ref = ArtifactRef(parsed.path, token, "document", formats[0], hashlib.sha256(local_url.encode()).hexdigest())
        elif parsed.path.startswith("/api/chat-images/") and len(segments) == 4 and (not parsed.query or parsed.query == "download=true"):
            token = segments[3]
            ref = ArtifactRef(parsed.path, token, "image", None, hashlib.sha256(local_url.encode()).hexdigest())
        else:
            continue
        if ref.key not in seen:
            found.append(ref)
            seen.add(ref.key)
    return found


def _resolve_file(ref: ArtifactRef, user, settings) -> Path:
    try:
        if ref.kind == "document":
            return resolve_chat_export(
                ref.token,
                ref.file_format or "",
                settings.rag_data_dir / "exports",
                user.id,
                settings.master_key_bytes,
            )
        return resolve_chat_image(
            ref.token,
            settings.rag_data_dir / "generated_images",
            user.id,
            settings.master_key_bytes,
        )
    except HTTPException:
        raise ValueError("artifact_unavailable") from None


async def _load_file(ref: ArtifactRef, user, settings) -> ArtifactFile:
    path = _resolve_file(ref, user, settings)
    limit = MAX_IMAGE_WHATSAPP_BYTES if ref.kind == "image" else MAX_DOCUMENT_WHATSAPP_BYTES
    filename = "infraspine-generated-image.png" if ref.kind == "image" else f"infraspine-chat-response.{ref.file_format}"
    media_type = "image/png" if ref.kind == "image" else EXPORT_MEDIA_TYPES[ref.file_format or ""]
    try:
        size = path.stat().st_size
        if size < 1 or size > limit:
            raise ValueError("artifact_exceeds_whatsapp_limit")
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise ValueError("artifact_unavailable") from None
    if len(body) != size or len(body) > limit:
        raise ValueError("artifact_exceeds_whatsapp_limit")
    return ArtifactFile(filename, media_type, body)


async def _upload_media(connection, cipher, file: ArtifactFile) -> str:
    response = await provider_request(
        "https://graph.facebook.com/" + connection.api_version,
        "/" + connection.phone_number_id + "/media",
        method="POST",
        write=True,
        token=legacy._secret(connection, cipher, "access_token"),
        form={"messaging_product": "whatsapp", "type": file.media_type},
        files={"file": (file.filename, file.body, file.media_type)},
    )
    try:
        media_id = response.body["id"]
        if not isinstance(media_id, str) or not 1 <= len(media_id) <= 160:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise TransportError("missing_media_identifier", ambiguous=True) from None
    return media_id


async def _send_media(connection, inbound, cipher, artifact: ArtifactFile, media_id: str) -> str:
    kind = "image" if artifact.media_type.startswith("image/") else "document"
    media: dict[str, str] = {"id": media_id}
    if kind == "document":
        media["filename"] = artifact.filename
    response = await provider_request(
        "https://graph.facebook.com/" + connection.api_version,
        "/" + connection.phone_number_id + "/messages",
        method="POST",
        write=True,
        token=legacy._secret(connection, cipher, "access_token"),
        payload={"messaging_product": "whatsapp", "to": inbound.from_wa_id, "type": kind, kind: media},
    )
    try:
        provider_id = response.body["messages"][0]["id"]
        if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 160:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise TransportError("missing_message_identifier", ambiguous=True) from None
    return provider_id


async def deliver_artifacts(db: AsyncSession, connection, inbound, user, settings, cipher, answer: str) -> dict[str, int]:
    refs = artifact_refs(answer)
    if not refs:
        return {"total": 0, "sent": 0, "failed": 0}
    sent = failed = 0
    for ref in refs[:MAX_ARTIFACTS_PER_MESSAGE]:
        row = await db.scalar(
            select(WhatsAppArtifactDispatch)
            .where(WhatsAppArtifactDispatch.inbound_id == inbound.id, WhatsAppArtifactDispatch.artifact_key == ref.key)
            .with_for_update()
        )
        if row is None:
            try:
                file = await _load_file(ref, user, settings)
            except ValueError as exc:
                file = None
                error_code = str(exc)[:80]
            row = WhatsAppArtifactDispatch(
                inbound_id=inbound.id,
                artifact_key=ref.key,
                filename=file.filename if file else f"output.{ref.file_format or 'png'}",
                media_type=file.media_type if file else ("image/png" if ref.kind == "image" else EXPORT_MEDIA_TYPES.get(ref.file_format or "", "application/octet-stream")),
                status="failed" if file is None else "queued",
                last_error=None if file else error_code,
            )
            db.add(row)
            await db.flush()
        elif row.status == "accepted":
            sent += 1
            continue
        elif row.status in {"failed", "uncertain"}:
            failed += 1
            continue
        elif row.status == "uploaded":
            file = ArtifactFile(row.filename, row.media_type, b"")
        else:
            try:
                file = await _load_file(ref, user, settings)
            except ValueError as exc:
                row.status = "failed"
                row.last_error = str(exc)[:80]
                await db.commit()
                failed += 1
                continue

        if row.status == "failed":
            await db.commit()
            failed += 1
            continue
        if row.status == "queued":
            row.status = "uploading"
            row.last_error = None
            await db.commit()
            try:
                row.media_id = await _upload_media(connection, cipher, file)
            except TransportError as exc:
                row.status = "queued" if exc.code == "rate_limited" else ("uncertain" if exc.ambiguous else "failed")
                row.last_error = exc.code
                await db.commit()
                if exc.code == "rate_limited":
                    raise
                failed += 1
                continue
            row.status = "uploaded"
            await db.commit()
        if row.status == "uploaded":
            row.status = "sending"
            await db.commit()
            try:
                row.provider_message_id = await _send_media(connection, inbound, cipher, file, row.media_id or "")
            except TransportError as exc:
                row.status = "uploaded" if exc.code == "rate_limited" else ("uncertain" if exc.ambiguous else "failed")
                row.last_error = exc.code
                await db.commit()
                if exc.code == "rate_limited":
                    raise
                failed += 1
                continue
            row.status = "accepted"
            row.delivery_status = "accepted"
            row.last_error = None
            await db.commit()
            sent += 1
    failed += max(0, len(refs) - MAX_ARTIFACTS_PER_MESSAGE)
    return {"total": len(refs), "sent": sent, "failed": failed}


def artifact_reply(reply: str, results: dict[str, int]) -> str:
    if not results["total"]:
        return reply
    # Signed download tokens are bearer links. After delivery, keep only the visible filename/label in WhatsApp.
    visible = _MARKDOWN_LINK.sub(lambda match: re.match(r"!?\[([^\]]*)\]", match.group(0)).group(1), reply)
    answer = visible.removeprefix("[SUCCESS] ").strip()
    if results["failed"]:
        status = f"[PARTIAL] Agent task completed; {results['failed']} output file(s) could not be delivered to WhatsApp."
    else:
        status = f"[SUCCESS] Agent task completed. WhatsApp accepted {results['sent']} output file(s) for delivery."
    return f"{status}\n\n{answer}"[:12000]
