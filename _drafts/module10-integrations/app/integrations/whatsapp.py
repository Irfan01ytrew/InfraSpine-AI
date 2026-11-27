"""Official Cloud adapter with crash fencing, service-window checks and receipts.

Reuses the existing encrypted inbox, identity mapping and Agent Studio policy.
Uncertain task execution and sends are held for review instead of duplicated.
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import DbSession
from app.agent_studio.service import AgentStudioService, create_thread
from app.core.errors import InvalidInputError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection as Connection, AgentStudioWhatsAppContact as Contact, AgentStudioWhatsAppInbound as Inbound
from app.db.models import User
from app.integrations.models import WhatsAppDispatch as Dispatch
from app.integrations.transport import request as provider_request, TransportError
from app.services import agent_studio_whatsapp as legacy, audit
from app.services.audit import AuditContext

router = APIRouter(prefix="/api/agent-studio/whatsapp", tags=["whatsapp-cloud"])


def event_values(payload, connection):
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict) or str(entry.get("id")) != connection.business_account_id: continue
        for change in entry.get("changes", []):
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict): continue
            metadata = value.get("metadata")
            if isinstance(metadata, dict) and str(metadata.get("phone_number_id")) == connection.phone_number_id:
                yield value


async def accept(db, connection_id, raw, signature, cipher):
    accepted = await legacy.accept_webhook(db, connection_id, raw, signature, cipher)
    connection = await db.get(Connection, connection_id)
    payload = json.loads(raw)
    for value in event_values(payload, connection):
        for item in value.get("messages", []) if isinstance(value.get("messages"), list) else []:
            if not isinstance(item, dict): continue
            inbound = await db.scalar(select(Inbound).where(Inbound.connection_id == connection_id,
                Inbound.owner_id == connection.owner_id, Inbound.external_message_id == item.get("id")))
            if inbound is None: continue
            try:
                incoming = datetime.fromtimestamp(int(item.get("timestamp", "")), UTC)
                if incoming > datetime.now(UTC) + timedelta(minutes=5): incoming = None
            except (ValueError, OverflowError, TypeError, OSError): incoming = None
            await db.execute(pg_insert(Dispatch).values(inbound_id=inbound.id, phase="waiting", incoming_at=incoming)
                             .on_conflict_do_nothing(index_elements=["inbound_id"]))
        for receipt in value.get("statuses", []) if isinstance(value.get("statuses"), list) else []:
            if not isinstance(receipt, dict): continue
            row = await db.scalar(select(Dispatch).join(Inbound, Inbound.id == Dispatch.inbound_id).where(
                Inbound.connection_id == connection_id, Inbound.owner_id == connection.owner_id,
                Inbound.from_wa_id == str(receipt.get("recipient_id", "")), Dispatch.provider_message_id == receipt.get("id"),
            ).with_for_update(of=Dispatch))
            if row is None: continue
            status = receipt.get("status")
            rank = {None: 0, "accepted": 0, "failed": 1, "sent": 2, "delivered": 3, "read": 4}
            if status in rank and rank[status] > rank.get(row.delivery_status, 0):
                row.delivery_status = status; row.receipt_at = datetime.now(UTC)
                audit.record(db, AuditContext(connection.owner_id, None, None, None), "integration.whatsapp_receipt",
                             target=str(row.inbound_id), details={"status": status})
    await db.commit()
    return accepted


@router.get("/webhook/{connection_id}", response_class=PlainTextResponse, include_in_schema=False)
async def challenge(connection_id: uuid.UUID, request: Request, db: DbSession):
    from app.api.routes.agent_studio_whatsapp import verify_meta_webhook
    return await verify_meta_webhook(connection_id, request, db)


@router.post("/webhook/{connection_id}", include_in_schema=False)
async def receive(connection_id: uuid.UUID, request: Request, db: DbSession):
    from app.api.routes.integrations import body
    raw = await body(request, legacy.MAX_WEBHOOK_BYTES)
    await accept(db, connection_id, raw, request.headers.get("X-Hub-Signature-256", ""), request.app.state.cipher)
    return {"received": True}


async def claim(sessionmaker):
    async with sessionmaker() as db:
        now = datetime.now(UTC)
        stale = (await db.scalars(select(Inbound).where(Inbound.status == "processing", Inbound.claimed_at < now - timedelta(minutes=10))
                                 .with_for_update(skip_locked=True).limit(50))).all()
        for inbound in stale:
            inbound.status = "failed"; inbound.last_error = "execution_or_send_interrupted"
            dispatch = await db.get(Dispatch, inbound.id)
            if dispatch: dispatch.phase = "uncertain"; dispatch.claim_token = None
        inbound = await db.scalar(select(Inbound).join(Dispatch, Dispatch.inbound_id == Inbound.id).where(
            Inbound.status == "queued", Inbound.next_attempt_at <= now, Inbound.attempts < 5,
            Dispatch.phase.in_(("waiting", "prepared")),
        ).order_by(Inbound.created_at).with_for_update(of=Inbound, skip_locked=True).limit(1))
        if inbound is None: await db.commit(); return None
        dispatch = await db.get(Dispatch, inbound.id)
        inbound.status = "processing"; inbound.claimed_at = now; inbound.attempts += 1
        dispatch.claim_token = uuid.uuid4()
        if not inbound.reply_ciphertext: dispatch.phase = "executing"
        identity = inbound.id, dispatch.claim_token
        await db.commit()
        return identity


async def locked(db, identity):
    inbound = await db.scalar(select(Inbound).where(Inbound.id == identity[0]).with_for_update().execution_options(populate_existing=True))
    dispatch = await db.get(Dispatch, identity[0], populate_existing=True)
    if not inbound or not dispatch or inbound.status != "processing" or dispatch.claim_token != identity[1] or inbound.claimed_at < datetime.now(UTC) - timedelta(minutes=10):
        return None, None
    return inbound, dispatch


async def bound_identity(db, inbound):
    connection = await db.scalar(select(Connection).where(Connection.id == inbound.connection_id,
        Connection.owner_id == inbound.owner_id, Connection.enabled.is_(True)).with_for_update())
    user = await db.scalar(select(User).where(User.id == inbound.owner_id, User.is_active.is_(True)).with_for_update())
    if connection is None or user is None or inbound.from_wa_id not in connection.allowed_senders:
        raise TransportError("identity_revoked")
    agent = await db.scalar(select(AgentStudioAgent).where(AgentStudioAgent.id == connection.agent_id,
        AgentStudioAgent.owner_id == inbound.owner_id, AgentStudioAgent.enabled.is_(True)))
    if agent is None: raise TransportError("agent_unavailable")
    return connection, user, agent


async def process(sessionmaker, identity, settings, cipher, runtime, rag):
    error = None
    try:
        async with sessionmaker() as db:
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, user, agent = await bound_identity(db, inbound)
            if not dispatch.incoming_at or datetime.now(UTC) >= dispatch.incoming_at + timedelta(hours=24):
                raise TransportError("messaging_window_expired")
            if inbound.reply_ciphertext:
                reply = legacy._open(cipher, "reply", inbound.id, inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id)
            elif inbound.message_type != "text":
                reply = "This connection currently accepts text tasks. Please send your request as text or contact a human operator for this attachment."
            else:
                contact = await db.scalar(select(Contact).where(Contact.connection_id == connection.id,
                    Contact.owner_id == user.id, Contact.wa_id == inbound.from_wa_id))
                if contact is None:
                    thread = await create_thread(db, user, cipher, agent.id, "WhatsApp · " + inbound.from_wa_id[-4:])
                    await db.execute(pg_insert(Contact).values(id=uuid.uuid4(), connection_id=connection.id, owner_id=user.id,
                        wa_id=inbound.from_wa_id, thread_id=thread.id).on_conflict_do_nothing(index_elements=["connection_id", "wa_id"]))
                    await db.commit()
                    contact = await db.scalar(select(Contact).where(Contact.connection_id == connection.id,
                        Contact.owner_id == user.id, Contact.wa_id == inbound.from_wa_id))
                message = legacy._open(cipher, "inbound", inbound.id, inbound.text_ciphertext, inbound.text_nonce, inbound.text_key_id)
                run, state, _ = await AgentStudioService(db, user, settings, cipher, runtime, rag).start_run(agent.id, contact.thread_id, message)
                reply = ("This task requires approval in InfraSpine AI → AI Agents → Approvals & execution log."
                         if run.status == "awaiting_approval" else str(state.get("answer") or "Check the AI Agents execution log for this task's result."))
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, _, _ = await bound_identity(db, inbound)
            if datetime.now(UTC) >= dispatch.incoming_at + timedelta(hours=24): raise TransportError("messaging_window_expired")
            encrypted = legacy._seal(cipher, "reply", inbound.id, reply[:12000])
            inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id = encrypted.ciphertext, encrypted.nonce, encrypted.key_id
            dispatch.phase = "sending"
            await db.commit()
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, _, _ = await bound_identity(db, inbound)
            response = await provider_request("https://graph.facebook.com/" + connection.api_version,
                "/" + connection.phone_number_id + "/messages", method="POST", write=True,
                token=legacy._secret(connection, cipher, "access_token"), payload={"messaging_product": "whatsapp",
                "to": inbound.from_wa_id, "type": "text", "text": {"preview_url": False, "body": reply.strip()[:4000]}})
            try:
                provider_id = response.body["messages"][0]["id"]
                if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 160: raise ValueError()
            except (KeyError, IndexError, TypeError, ValueError):
                raise TransportError("missing_message_identifier", ambiguous=True) from None
            dispatch.provider_message_id = provider_id; dispatch.phase = "accepted"; dispatch.delivery_status = "accepted"
            dispatch.claim_token = None; inbound.status = "completed"; inbound.claimed_at = None; inbound.last_error = None
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_accepted", target=str(inbound.id))
            await db.commit()
    except TransportError as exc: error = exc
    except Exception: error = TransportError("execution_or_send_interrupted", ambiguous=True)
    if error:
        async with sessionmaker() as db:
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            retry = error.code == "rate_limited" and inbound.reply_ciphertext is not None and inbound.attempts < 5
            inbound.status = "queued" if retry else "failed"
            inbound.last_error = error.code; inbound.claimed_at = None
            inbound.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(30, error.retry_after))
            dispatch.phase = "prepared" if retry else ("uncertain" if error.ambiguous else "failed")
            dispatch.claim_token = None
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_failed", target=str(inbound.id),
                         details={"error": error.code, "phase": dispatch.phase})
            await db.commit()


async def run_whatsapp_agent_worker(sessionmaker, settings, cipher, runtime, rag, stop):
    while not stop.is_set():
        try:
            identity = await claim(sessionmaker)
            if identity:
                await process(sessionmaker, identity, settings, cipher, runtime, rag)
                continue
        except asyncio.CancelledError: raise
        except Exception: pass  # no raw payloads or credential-bearing exceptions in logs
        try: await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError: pass
