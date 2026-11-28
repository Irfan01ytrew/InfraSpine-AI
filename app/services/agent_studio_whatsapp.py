"""WhatsApp Business Cloud integration for independently scoped Agent Studio agents."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_studio.service import AgentStudioService, create_thread
from app.core.config import Settings
from app.core.crypto import CredentialCipher, EncryptedValue
from app.core.errors import InvalidInputError, NotFoundError
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioThread
from app.db.agent_studio_whatsapp_models import (
    AgentStudioWhatsAppConnection,
    AgentStudioWhatsAppContact,
    AgentStudioWhatsAppInbound,
)
from app.db.models import User
from app.services.llm_runtime import LLMRuntime

logger = logging.getLogger(__name__)
_META_ID = re.compile(r"^[0-9]{5,64}$")
_SENDER_ID = re.compile(r"^[0-9]{8,16}$")
_API_VERSION = re.compile(r"^v[0-9]{1,2}\.0$")
MAX_WEBHOOK_BYTES = 1_000_000
_QUEUE_POLL_SECONDS = 1.0


def _aad(kind: str, row_id: uuid.UUID) -> bytes:
    return f"infraspine:agent-studio-whatsapp:v1:{kind}:{row_id}".encode("ascii")


def _seal(cipher: CredentialCipher, kind: str, row_id: uuid.UUID, value: str) -> EncryptedValue:
    return cipher.encrypt(value, associated_data=_aad(kind, row_id))


def _open(cipher: CredentialCipher, kind: str, row_id: uuid.UUID, ciphertext: bytes, nonce: bytes, key_id: str) -> str:
    return cipher.decrypt(EncryptedValue(ciphertext, nonce, key_id), associated_data=_aad(kind, row_id))


def _secret(connection: AgentStudioWhatsAppConnection, cipher: CredentialCipher, name: str) -> str:
    return _open(
        cipher,
        name,
        connection.id,
        getattr(connection, f"{name}_ciphertext"),
        getattr(connection, f"{name}_nonce"),
        getattr(connection, f"{name}_key_id"),
    )


def webhook_url(connection_id: uuid.UUID, request_base: str, configured_base: str = "") -> str:
    base = configured_base.strip() or request_base.strip()
    return f"{base.rstrip('/')}/api/agent-studio/whatsapp/webhook/{connection_id}"


async def get_connection(db: AsyncSession, user: User) -> AgentStudioWhatsAppConnection | None:
    return await db.scalar(select(AgentStudioWhatsAppConnection).where(AgentStudioWhatsAppConnection.owner_id == user.id))


def public_connection(
    connection: AgentStudioWhatsAppConnection | None,
    agent_name: str | None,
    request_base: str,
    configured_base: str = "",
) -> dict[str, Any] | None:
    if connection is None:
        return None
    return {
        "id": str(connection.id),
        "agent_id": str(connection.agent_id),
        "agent_name": agent_name or "Unavailable agent",
        "phone_number_id": connection.phone_number_id,
        "business_account_id": connection.business_account_id,
        "allowed_senders": list(connection.allowed_senders or []),
        "api_version": connection.api_version,
        "enabled": connection.enabled,
        "verified_name": connection.verified_name,
        "display_phone_number": connection.display_phone_number,
        "last_verified_at": connection.last_verified_at,
        "access_token_configured": bool(connection.access_token_ciphertext),
        "app_secret_configured": bool(connection.app_secret_ciphertext),
        "verify_token_configured": bool(connection.verify_token_ciphertext),
        "webhook_url": webhook_url(connection.id, request_base, configured_base),
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


async def save_connection(
    db: AsyncSession,
    user: User,
    cipher: CredentialCipher,
    data: dict[str, Any],
) -> AgentStudioWhatsAppConnection:
    agent_id = data.get("agent_id")
    agent = await db.scalar(select(AgentStudioAgent).where(
        AgentStudioAgent.id == agent_id,
        AgentStudioAgent.owner_id == user.id,
        AgentStudioAgent.enabled.is_(True),
    ))
    if agent is None:
        raise InvalidInputError("Choose an enabled agent owned by your account.")

    phone_number_id = str(data.get("phone_number_id") or "").strip()
    business_account_id = str(data.get("business_account_id") or "").strip()
    allowed_senders = list(dict.fromkeys(re.sub(r"\D", "", str(item)) for item in data.get("allowed_senders") or []))
    api_version = str(data.get("api_version") or "v26.0").strip()
    if not _META_ID.fullmatch(phone_number_id) or not _META_ID.fullmatch(business_account_id):
        raise InvalidInputError("Enter the numeric WhatsApp phone-number ID and Business Account ID from Meta.")
    if not _API_VERSION.fullmatch(api_version):
        raise InvalidInputError("Graph API version must look like v26.0.")
    if not allowed_senders or len(allowed_senders) > 30 or any(not _SENDER_ID.fullmatch(item) for item in allowed_senders):
        raise InvalidInputError("Add between 1 and 30 allowed WhatsApp numbers with country code, using digits only.")

    secrets = {
        "access_token": str(data.get("access_token") or "").strip(),
        "app_secret": str(data.get("app_secret") or "").strip(),
        "verify_token": str(data.get("verify_token") or "").strip(),
    }
    row = await db.scalar(select(AgentStudioWhatsAppConnection).where(
        AgentStudioWhatsAppConnection.owner_id == user.id
    ).with_for_update())
    if row is None:
        if not all(secrets.values()):
            raise InvalidInputError("Enter the access token, app secret and webhook verify token to create the connection.")
        row = AgentStudioWhatsAppConnection(
            id=uuid.uuid4(),
            owner_id=user.id,
            agent_id=agent.id,
            phone_number_id=phone_number_id,
            business_account_id=business_account_id,
            allowed_senders=allowed_senders,
            api_version=api_version,
            # temporary placeholders are replaced before the row is flushed
            access_token_ciphertext=b"pending", access_token_nonce=b"pending", access_token_key_id="pending",
            app_secret_ciphertext=b"pending", app_secret_nonce=b"pending", app_secret_key_id="pending",
            verify_token_ciphertext=b"pending", verify_token_nonce=b"pending", verify_token_key_id="pending",
        )
        db.add(row)
    for name, value in secrets.items():
        if value:
            if len(value) > 10_000 or any(ord(char) < 32 for char in value):
                raise InvalidInputError(f"The {name.replace('_', ' ')} is invalid or too long.")
            sealed = _seal(cipher, name, row.id, value)
            setattr(row, f"{name}_ciphertext", sealed.ciphertext)
            setattr(row, f"{name}_nonce", sealed.nonce)
            setattr(row, f"{name}_key_id", sealed.key_id)
        elif not getattr(row, f"{name}_ciphertext"):
            raise InvalidInputError(f"Enter the WhatsApp {name.replace('_', ' ')} to connect this number.")

    row.agent_id = agent.id
    row.phone_number_id = phone_number_id
    row.business_account_id = business_account_id
    row.allowed_senders = allowed_senders
    row.api_version = api_version
    row.enabled = True
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


async def _meta_request(
    connection: AgentStudioWhatsAppConnection,
    cipher: CredentialCipher,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{connection.api_version}/{path.lstrip('/')}"
    token = _secret(connection, cipher, "access_token")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=False) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise InvalidInputError("Could not reach Meta's WhatsApp API. Check server internet access and try again.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict):
        detail = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None
        safe_detail = " ".join(str(detail or "Meta rejected the request.").split())[:300]
        raise InvalidInputError(f"WhatsApp Cloud API error ({response.status_code}): {safe_detail}")
    return body


async def verify_and_subscribe(
    db: AsyncSession, connection: AgentStudioWhatsAppConnection, cipher: CredentialCipher
) -> dict[str, str | None]:
    details = await _meta_request(
        connection,
        cipher,
        "GET",
        connection.phone_number_id,
        params={"fields": "id,display_phone_number,verified_name"},
    )
    if str(details.get("id")) != connection.phone_number_id:
        raise InvalidInputError("Meta returned a different phone-number ID. Check the configured credentials.")
    await _meta_request(
        connection,
        cipher,
        "POST",
        f"{connection.business_account_id}/subscribed_apps",
        json_body={},
    )
    connection.verified_name = str(details.get("verified_name") or "")[:200] or None
    connection.display_phone_number = str(details.get("display_phone_number") or "")[:40] or None
    connection.last_verified_at = datetime.now(UTC)
    connection.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(connection)
    return {"verified_name": connection.verified_name, "display_phone_number": connection.display_phone_number}


async def delete_connection(db: AsyncSession, user: User) -> bool:
    row = await get_connection(db, user)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def _load_by_id(db: AsyncSession, connection_id: uuid.UUID) -> AgentStudioWhatsAppConnection | None:
    return await db.get(AgentStudioWhatsAppConnection, connection_id)


async def verify_webhook_request(
    db: AsyncSession, connection_id: uuid.UUID, supplied_token: str, cipher: CredentialCipher
) -> bool:
    connection = await _load_by_id(db, connection_id)
    if connection is None or not connection.enabled:
        return False
    try:
        expected = _secret(connection, cipher, "verify_token")
    except Exception:
        logger.error("WhatsApp webhook verify token could not be decrypted connection_id=%s", connection_id)
        return False
    return hmac.compare_digest(expected, supplied_token)


def _incoming_events(payload: dict[str, Any], connection: AgentStudioWhatsAppConnection) -> list[tuple[str, str, str, str]]:
    events: list[tuple[str, str, str, str]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        if not isinstance(entry, dict) or str(entry.get("id") or "") != connection.business_account_id:
            continue
        for change in entry.get("changes", []) if isinstance(entry.get("changes"), list) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            if str(metadata.get("phone_number_id") or "") != connection.phone_number_id:
                continue
            messages = value.get("messages") if isinstance(value.get("messages"), list) else []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                external_id = item.get("id")
                sender = item.get("from")
                kind = str(item.get("type") or "unknown")[:24]
                if not isinstance(external_id, str) or not 1 <= len(external_id) <= 160:
                    continue
                if not isinstance(sender, str) or sender not in (connection.allowed_senders or []):
                    continue
                text = ""
                if kind == "text" and isinstance(item.get("text"), dict):
                    text = str(item["text"].get("body") or "")[:6000]
                elif kind == "interactive" and isinstance(item.get("interactive"), dict):
                    interaction = item["interactive"]
                    reply = interaction.get("button_reply") or interaction.get("list_reply") or {}
                    if not isinstance(reply, dict):
                        continue
                    text = str(reply.get("title") or reply.get("id") or "")[:6000]
                    kind = "text"
                events.append((external_id, sender, kind, text))
    return events


async def accept_webhook(
    db: AsyncSession,
    connection_id: uuid.UUID,
    raw_body: bytes,
    signature: str,
    cipher: CredentialCipher,
) -> int:
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise InvalidInputError("WhatsApp webhook payload is too large.")
    if re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None:
        raise InvalidInputError("WhatsApp webhook signature is invalid.")
    connection = await _load_by_id(db, connection_id)
    if connection is None or not connection.enabled:
        raise NotFoundError("WhatsApp integration not found.")
    secret = _secret(connection, cipher, "app_secret").encode("utf-8")
    expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidInputError("WhatsApp webhook signature is invalid.")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInputError("WhatsApp webhook body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidInputError("WhatsApp webhook body must be a JSON object.")

    accepted = 0
    for external_id, sender, kind, text in _incoming_events(payload, connection):
        inbound_id = uuid.uuid4()
        stored_text = text if kind == "text" else ""
        sealed = _seal(cipher, "inbound", inbound_id, stored_text)
        result = await db.execute(
            pg_insert(AgentStudioWhatsAppInbound)
            .values(
                id=inbound_id,
                connection_id=connection.id,
                owner_id=connection.owner_id,
                external_message_id=external_id,
                from_wa_id=sender,
                message_type=kind,
                text_ciphertext=sealed.ciphertext,
                text_nonce=sealed.nonce,
                text_key_id=sealed.key_id,
                status="queued",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["external_message_id"])
            .returning(AgentStudioWhatsAppInbound.id)
        )
        accepted += int(result.scalar_one_or_none() is not None)
    await db.commit()
    return accepted


async def _send_text(
    connection: AgentStudioWhatsAppConnection,
    cipher: CredentialCipher,
    recipient: str,
    body: str,
) -> None:
    text = body.strip()[:4000] or "I could not prepare a response. Please try again."
    await _meta_request(
        connection,
        cipher,
        "POST",
        f"{connection.phone_number_id}/messages",
        json_body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        },
    )


async def _claim_one(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    async with sessionmaker() as db:
        # Recover work left in flight by a stopped worker; the outbound reply is persisted before delivery,
        # so retrying after a restart never reruns the LLM task.
        stale = datetime.now(UTC) - timedelta(minutes=5)
        await db.execute(
            update(AgentStudioWhatsAppInbound)
            .where(AgentStudioWhatsAppInbound.status == "processing", AgentStudioWhatsAppInbound.claimed_at < stale)
            .values(status="queued", claimed_at=None, next_attempt_at=datetime.now(UTC))
        )
        row = await db.scalar(
            select(AgentStudioWhatsAppInbound)
            .where(
                AgentStudioWhatsAppInbound.status == "queued",
                AgentStudioWhatsAppInbound.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(AgentStudioWhatsAppInbound.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            await db.commit()
            return None
        row.status = "processing"
        row.claimed_at = datetime.now(UTC)
        row.attempts += 1
        row.updated_at = datetime.now(UTC)
        await db.commit()
        return row.id


async def _process_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    inbound_id: uuid.UUID,
    settings: Settings,
    cipher: CredentialCipher,
    runtime: LLMRuntime,
    rag,
) -> None:
    async with sessionmaker() as db:
        inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
        if inbound is None or inbound.status != "processing":
            return
        connection = await db.get(AgentStudioWhatsAppConnection, inbound.connection_id)
        user = await db.get(User, inbound.owner_id)
        if connection is None or user is None or not connection.enabled:
            inbound.status = "failed"
            inbound.last_error = "integration_unavailable"
            inbound.updated_at = datetime.now(UTC)
            await db.commit()
            return

        try:
            if inbound.reply_ciphertext is not None:
                reply = _open(cipher, "reply", inbound.id, inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id)
            elif inbound.message_type != "text":
                reply = "I received your WhatsApp attachment, but this connection is currently set up for text requests. Please send your task in a text message."
            else:
                message = _open(cipher, "inbound", inbound.id, inbound.text_ciphertext, inbound.text_nonce, inbound.text_key_id)
                agent = await db.scalar(select(AgentStudioAgent).where(
                    AgentStudioAgent.id == connection.agent_id,
                    AgentStudioAgent.owner_id == user.id,
                    AgentStudioAgent.enabled.is_(True),
                ))
                if agent is None:
                    raise RuntimeError("configured_agent_unavailable")
                contact = await db.scalar(select(AgentStudioWhatsAppContact).where(
                    AgentStudioWhatsAppContact.connection_id == connection.id,
                    AgentStudioWhatsAppContact.wa_id == inbound.from_wa_id,
                ))
                if contact is None:
                    thread = await create_thread(
                        db, user, cipher, agent.id, f"WhatsApp · {inbound.from_wa_id[-4:]}"
                    )
                    await db.execute(
                        pg_insert(AgentStudioWhatsAppContact)
                        .values(
                            id=uuid.uuid4(), connection_id=connection.id, owner_id=user.id,
                            wa_id=inbound.from_wa_id, thread_id=thread.id,
                        )
                        .on_conflict_do_nothing(index_elements=["connection_id", "wa_id"])
                    )
                    await db.commit()
                    contact = await db.scalar(select(AgentStudioWhatsAppContact).where(
                        AgentStudioWhatsAppContact.connection_id == connection.id,
                        AgentStudioWhatsAppContact.wa_id == inbound.from_wa_id,
                    ))
                if contact is None:
                    raise RuntimeError("whatsapp_conversation_unavailable")
                run, state, _approval = await AgentStudioService(
                    db, user, settings, cipher, runtime, rag
                ).start_run(
                    agent.id,
                    contact.thread_id,
                    message,
                    idempotency_key=f"whatsapp-cloud:{inbound.id}",
                    execute_inline=True,
                )
                if run.status == "awaiting_approval":
                    reply = (
                        "This task needs your approval before the agent can continue. Open InfraSpine AI → "
                        "AI Agents → Approvals & execution log, approve or reject it, then message me again for the result."
                    )
                elif run.status == "completed":
                    reply = str(state.get("answer") or "The agent completed the task but did not return a text response.")
                else:
                    reply = "I couldn't complete that task right now. Please retry shortly or check the AI Agents execution log."

            sealed = _seal(cipher, "reply", inbound.id, reply[:12_000])
            inbound.reply_ciphertext = sealed.ciphertext
            inbound.reply_nonce = sealed.nonce
            inbound.reply_key_id = sealed.key_id
            inbound.updated_at = datetime.now(UTC)
            await db.commit()
            # Send from the persisted answer. A transient Graph API failure retries delivery without rerunning work.
            await _send_text(connection, cipher, inbound.from_wa_id, reply)
            inbound.status = "completed"
            inbound.last_error = None
            inbound.claimed_at = None
            inbound.updated_at = datetime.now(UTC)
            await db.commit()
        except Exception as exc:
            logger.exception("WhatsApp agent task failed inbound_id=%s error_type=%s", inbound_id, type(exc).__name__)
            await db.rollback()
            inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
            if inbound is None:
                return
            inbound.last_error = type(exc).__name__[:80]
            inbound.claimed_at = None
            inbound.updated_at = datetime.now(UTC)
            if inbound.attempts >= 5:
                inbound.status = "failed"
            else:
                inbound.status = "queued"
                inbound.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(120, 5 * (2 ** inbound.attempts)))
            await db.commit()


async def run_whatsapp_agent_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cipher: CredentialCipher,
    runtime: LLMRuntime,
    rag,
    stop: asyncio.Event,
) -> None:
    """Durably execute and reply to incoming Meta webhooks without holding the webhook request open."""
    logger.info("Agent Studio WhatsApp worker started")
    while not stop.is_set():
        try:
            inbound_id = await _claim_one(sessionmaker)
            if inbound_id is not None:
                await _process_one(sessionmaker, inbound_id, settings, cipher, runtime, rag)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("WhatsApp queue poll failed error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_QUEUE_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Agent Studio WhatsApp worker stopped")
