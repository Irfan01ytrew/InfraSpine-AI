"""Local Baileys processes, encrypted auth state, and owner-only agent execution.

The Node subprocess has no HTTP API. Auth snapshots cross private pipes and are
acknowledged only after AES-GCM encrypted storage commits. Commands are durable;
an interrupted agent action is never automatically replayed.
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.agent_studio.service import AgentStudioService, create_thread
from app.agent_studio.host_status import windows_host_status
from app.core.crypto import EncryptedValue
from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import User

logger = logging.getLogger(__name__)
BRIDGE = Path(__file__).resolve().parents[2] / "integrations" / "whatsapp-baileys" / "bridge.mjs"
RECEIPTS = {2: "server_acknowledged", 3: "device_delivered", 4: "read"}


def bridge_available():
    return bool(shutil.which("node") and (BRIDGE.parent / "node_modules/@whiskeysockets/baileys/package.json").is_file())


def seal(cipher, identity, value):
    result = cipher.encrypt(value, associated_data=f"infraspine:baileys:v1:{identity}".encode())
    return json.dumps({"c": base64.b64encode(result.ciphertext).decode(), "n": base64.b64encode(result.nonce).decode(), "k": result.key_id})


def unseal(cipher, identity, value):
    data = json.loads(value)
    return cipher.decrypt(EncryptedValue(base64.b64decode(data["c"]), base64.b64decode(data["n"]), data["k"]), associated_data=f"infraspine:baileys:v1:{identity}".encode())


async def public_connection(db, user, cipher):
    row = await db.scalar(select(BaileysConnection).where(BaileysConnection.owner_id == user.id))
    result = {"available": bridge_available(), "connection": None}
    if row:
        now = datetime.now(UTC)
        live = bool(row.heartbeat_at and now - row.heartbeat_at < timedelta(seconds=45))
        qr = unseal(cipher, f"qr:{row.id}", row.qr_sealed) if row.enabled and row.qr_sealed and row.qr_expires_at and row.qr_expires_at > now and live else None
        result["connection"] = {
            "id": str(row.id), "agent_id": str(row.agent_id), "enabled": row.enabled,
            "status": row.status if live or not row.enabled else "starting",
            "phone_suffix": row.phone_suffix, "qr": qr,
            "last_error": row.last_error, "heartbeat_at": row.heartbeat_at,
        }
        latest = (await db.execute(
            select(BaileysMessage.id, BaileysMessage.status, BaileysMessage.receipt_status, BaileysMessage.created_at)
            .where(BaileysMessage.connection_id == row.id)
            .order_by(BaileysMessage.created_at.desc(), BaileysMessage.id.desc())
            .limit(1)
        )).one_or_none()
        result["connection"]["latest_command"] = {
            "id": str(latest.id), "status": latest.status,
            "receipt": RECEIPTS.get(latest.receipt_status), "received_at": latest.created_at,
        } if latest else None
    return result


async def record_receipt(db, connection_id, delivery_id, status):
    """Correlate monotonic receipts with replies owned by this connection."""
    if not isinstance(delivery_id, str) or not re.fullmatch(r"[0-9A-F]{32}", delivery_id):
        return
    if type(status) is not int or status not in RECEIPTS:
        return
    await db.execute(
        update(BaileysMessage)
        .where(
            BaileysMessage.id == uuid.UUID(hex=delivery_id),
            BaileysMessage.connection_id == connection_id,
            BaileysMessage.reply_sealed.is_not(None),
            BaileysMessage.status.in_(("sending", "completed", "failed", "interrupted")),
            or_(BaileysMessage.receipt_status.is_(None), BaileysMessage.receipt_status < status),
        )
        .values(receipt_status=status)
    )


class BridgeSession:
    def __init__(self, app, settings, connection_id):
        self.app, self.settings, self.id = app, settings, connection_id
        self.db = app.state.sessionmaker
        self.cipher = app.state.cipher
        self.process = None
        self.send_waiters = {}
        self.online = asyncio.Event()
        self.write_lock = asyncio.Lock()

    async def write(self, payload):
        async with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError("bridge_not_running")
            data = (json.dumps(payload) + "\n").encode()
            def send():
                self.process.stdin.write(data)
                self.process.stdin.flush()
            await asyncio.to_thread(send)

    async def active(self, db):
        row = await db.get(BaileysConnection, self.id)
        user = await db.get(User, row.owner_id) if row else None
        agent = await db.get(AgentStudioAgent, row.agent_id) if row else None
        if not row or not row.enabled or not user or not user.is_active or not agent or not agent.enabled or agent.owner_id != user.id:
            return None
        return row, user, agent

    async def run(self):
        consumer = None
        try:
            async with self.db() as db:
                active = await self.active(db)
                if not active:
                    return
                row, _, _ = active
                state = unseal(self.cipher, f"auth:{self.id}", row.auth_sealed) if row.auth_sealed else None
                # An interrupted tool run may already have side effects: never replay it.
                await db.execute(update(BaileysMessage).where(BaileysMessage.connection_id == self.id, BaileysMessage.status.in_(["processing", "sending"])).values(status="interrupted", last_error="Check execution log before resending this command."))
                row.status = "starting"
                row.last_error = None
                row.qr_sealed = None
                await db.commit()
            # Popen pipe I/O is offloaded for Windows SelectorEventLoop (uvicorn --reload).
            self.process = await asyncio.to_thread(subprocess.Popen,
                [shutil.which("node"), str(BRIDGE)], cwd=str(BRIDGE.parent),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            await self.write({"type": "init", "state": state})
            consumer = asyncio.create_task(self.consume())
            while line := await asyncio.to_thread(self.process.stdout.readline, 16 * 1024 * 1024):
                if len(line) >= 16 * 1024 * 1024:
                    raise RuntimeError("bridge_state_limit")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                await self.event(event)
            await asyncio.to_thread(self.process.wait)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Baileys bridge stopped connection=%s type=%s", self.id, type(exc).__name__)
            async with self.db() as db:
                await db.execute(update(BaileysConnection).where(BaileysConnection.id == self.id, BaileysConnection.enabled.is_(True)).values(status="error", last_error="WhatsApp bridge stopped. Reconnect from the dashboard."))
                await db.commit()
        finally:
            if consumer:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer
            if self.process and self.process.poll() is None:
                with contextlib.suppress(Exception):
                    await self.write({"type": "stop"})
                    await asyncio.to_thread(self.process.wait, 5)
                if self.process.poll() is None:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait)
            if self.process:
                self.process.stdin.close()
                self.process.stdout.close()

    async def event(self, event):
        kind = event.get("type")
        if kind in ("sent", "send_error"):
            waiter = self.send_waiters.get(event.get("id"))
            if waiter and not waiter.done():
                waiter.set_result(kind == "sent")
            return
        async with self.db() as db:
            active = await self.active(db)
            if not active:
                return
            row, _, _ = active
            row.heartbeat_at = datetime.now(UTC)
            if kind == "auth":
                row.auth_sealed = seal(self.cipher, f"auth:{self.id}", event["state"])
            elif kind == "qr":
                if not str(event.get("image", "")).startswith("data:image/png;base64,"):
                    return
                row.qr_sealed = seal(self.cipher, f"qr:{self.id}", event["image"])
                row.qr_expires_at = datetime.now(UTC) + timedelta(seconds=45)
                row.status = "scan_qr"
            elif kind == "status":
                value = event.get("status")
                if value not in {"connected", "logged_out", "reconnecting"}:
                    return
                row.status = value
                row.qr_sealed = None
                if value == "connected":
                    row.phone_suffix = str(event.get("suffix", ""))[:4]
                    self.online.set()
                else:
                    self.online.clear()
                if value == "logged_out":
                    row.auth_sealed = None
                    row.enabled = False
            elif kind == "error":
                row.status = "error"
                row.last_error = "WhatsApp connection failed. Check internet access, then reconnect."
                self.online.clear()
            elif kind == "receipt":
                await record_receipt(db, self.id, event.get("id"), event.get("status"))
            elif kind == "message":
                # The bridge admits only self-chat commands; validate/bound its IPC here too.
                external_id = str(event.get("id", ""))
                body = str(event.get("text", "")).strip()
                jid = str(event.get("jid", ""))
                if not external_id or len(external_id) > 100 or not body or len(body) > 12000 or not jid.endswith(("@s.whatsapp.net", "@lid")):
                    return
                pending = await db.scalar(select(BaileysMessage.id).where(BaileysMessage.connection_id == self.id, BaileysMessage.status.in_(["queued", "processing", "ready", "sending"])).offset(19).limit(1))
                if pending:
                    row.last_error = "Command queue is full; wait for the current tasks before sending more."
                else:
                    message_id = uuid.uuid4()
                    payload = seal(self.cipher, f"message:{message_id}", json.dumps({"text": body, "jid": jid}))
                    await db.execute(insert(BaileysMessage).values(id=message_id, connection_id=self.id, external_id=f"{self.id}:{external_id}", payload_sealed=payload, status="queued").on_conflict_do_nothing(index_elements=["external_id"]))
            await db.commit()
        if kind == "auth":
            await self.write({"type": "ack", "id": event["id"]})

    async def consume(self):
        while True:
            await self.online.wait()
            message_id = None
            async with self.db() as db:
                row = await db.scalar(select(BaileysMessage).where(BaileysMessage.connection_id == self.id, BaileysMessage.status.in_(["queued", "ready"])).order_by(BaileysMessage.created_at).with_for_update(skip_locked=True).limit(1))
                if row:
                    message_id = row.id
                    row.status = "processing" if not row.reply_sealed else "ready"
                    await db.commit()
            if message_id:
                try:
                    await asyncio.wait_for(self.execute(message_id), timeout=300)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with self.db() as db:
                        await db.execute(update(BaileysMessage).where(BaileysMessage.id == message_id).values(status="failed", last_error=type(exc).__name__[:100]))
                        await db.commit()
                    logger.warning("Baileys command failed id=%s type=%s", message_id, type(exc).__name__)
            else:
                await asyncio.sleep(1)

    async def execute(self, message_id):
        async with self.db() as db:
            active = await self.active(db)
            if not active:
                raise RuntimeError("connection_disabled")
            connection, user, agent = active
            message = await db.get(BaileysMessage, message_id)
            payload = json.loads(unseal(self.cipher, f"message:{message_id}", message.payload_sealed))
            if message.reply_sealed:
                reply = unseal(self.cipher, f"reply:{message_id}", message.reply_sealed)
            elif " ".join(payload["text"].casefold().split()) in {
                "laptop status", "laptop diagnostics", "laptop check karo", "mera laptop check karo",
            }:
                await db.commit()
                report = await asyncio.to_thread(windows_host_status)
                reply = f"InfraSpine AI · {agent.name}\n\n{report}"
                message.reply_sealed = seal(self.cipher, f"reply:{message_id}", reply)
                message.status = "ready"
                await db.commit()
            else:
                # Release the database connection while waiting for the transport.
                await db.commit()
                if not await self.send_reply(
                    uuid.uuid5(message_id, "ack").hex.upper(),
                    payload["jid"],
                    f"InfraSpine AI · {agent.name}\n\nRequest received. The result or next step will follow here.",
                    timeout=5,
                ):
                    logger.info("WhatsApp acknowledgement unavailable id=%s", message_id)
                if not connection.thread_id:
                    thread = await create_thread(db, user, self.cipher, agent.id, "WhatsApp · My agent")
                    connection.thread_id = thread.id
                    await db.commit()
                run, state, _ = await AgentStudioService(
                    db, user, self.settings, self.cipher, self.app.state.llm_runtime, self.app.state.rag
                ).start_run(
                    agent.id,
                    connection.thread_id,
                    payload["text"],
                    idempotency_key=f"baileys:{message_id}",
                    execute_inline=True,
                )
                if run.status == "awaiting_approval":
                    reply = "*Approval required*\nOpen InfraSpine AI → AI Agents → Approvals & execution log to review the proposed action."
                elif run.status == "completed":
                    reply = "*Result*\n" + str(state.get("answer") or "Open AI Agents for the execution details.")
                else:
                    reply = "*Task incomplete*\nCheck AI Agents → execution log for details."
                # Do not allow generated content to become a self-chat command.
                reply = f"InfraSpine AI · {agent.name}\n\n" + reply[:11500]
                message.reply_sealed = seal(self.cipher, f"reply:{message_id}", reply)
                message.status = "ready"
                await db.commit()
            # Recheck revocation after a potentially long model/tool call.
            await db.refresh(connection)
            if not connection.enabled:
                raise RuntimeError("connection_disabled")
            if not self.online.is_set():
                # The result is durable and no send started. Resume delivery after
                # reconnect without invoking the agent or running its tools again.
                return
            message.status = "sending"
            await db.commit()
            delivery_id = message_id.hex.upper()
            if not await self.send_reply(delivery_id, payload["jid"], reply, timeout=45):
                raise RuntimeError("delivery_failed")
            message.status = "completed"
            await db.commit()

    async def send_reply(self, delivery_id, jid, reply, *, timeout):
        waiter = asyncio.get_running_loop().create_future()
        self.send_waiters[delivery_id] = waiter
        try:
            async with asyncio.timeout(timeout):
                await self.write({"type": "send", "id": delivery_id, "jid": jid, "text": reply})
                return await waiter
        except (TimeoutError, RuntimeError, OSError):
            return False
        finally:
            self.send_waiters.pop(delivery_id, None)


async def run_baileys_worker(app, settings, stop):
    sessions = {}
    try:
        while not stop.is_set():
            try:
                stopped_ids = set()
                async with app.state.sessionmaker() as db:
                    rows = list((await db.scalars(select(BaileysConnection).join(User, User.id == BaileysConnection.owner_id).join(AgentStudioAgent, AgentStudioAgent.id == BaileysConnection.agent_id).where(BaileysConnection.enabled.is_(True), User.is_active.is_(True), AgentStudioAgent.enabled.is_(True)))).all())
                active_ids = {row.id for row in rows}
                for identity in list(sessions):
                    bridge, task = sessions[identity]
                    if identity not in active_ids:
                        with contextlib.suppress(Exception):
                            await bridge.write({"type": "logout"})
                            await asyncio.wait_for(asyncio.shield(task), 5)
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        del sessions[identity]
                    elif task.done():
                        # Explicit reconnect is required after terminal failures; no restart storm.
                        async with app.state.sessionmaker() as db:
                            await db.execute(update(BaileysConnection).where(BaileysConnection.id == identity).values(enabled=False, status="error", qr_sealed=None, last_error="Connection stopped. Reconnect to continue."))
                            await db.commit()
                        del sessions[identity]
                        stopped_ids.add(identity)
                for row in rows:
                    if row.id not in sessions and row.id not in stopped_ids and bridge_available():
                        bridge = BridgeSession(app, settings, row.id)
                        sessions[row.id] = (bridge, asyncio.create_task(bridge.run()))
            except Exception as exc:
                logger.warning("Baileys supervisor error type=%s", type(exc).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 2)
    finally:
        for _, task in sessions.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in sessions.values()), return_exceptions=True)
