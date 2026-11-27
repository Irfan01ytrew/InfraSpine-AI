"""Authenticated, owner-scoped QR connector administration."""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession, get_current_user, audit_context
from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage
from app.db.agent_studio_models import AgentStudioAgent
from app.services import audit
from app.services.agent_studio_baileys import bridge_available, public_connection

router = APIRouter(prefix="/api/agent-studio/whatsapp-qr", tags=["agent-studio"], dependencies=[Depends(get_current_user)])


class ConnectRequest(BaseModel):
    agent_id: uuid.UUID


@router.get("")
async def status(request: Request, response: Response, db: DbSession, user: CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    return await public_connection(db, user, request.app.state.cipher)


@router.post("")
async def connect(payload: ConnectRequest, request: Request, db: DbSession, user: CurrentUser):
    if not bridge_available():
        raise HTTPException(503, "Install the WhatsApp bridge dependencies with npm ci in integrations/whatsapp-baileys.")
    agent = await db.scalar(select(AgentStudioAgent).where(AgentStudioAgent.id == payload.agent_id, AgentStudioAgent.owner_id == user.id, AgentStudioAgent.enabled.is_(True)))
    if agent is None:
        raise HTTPException(404, "Choose an enabled agent owned by your account.")
    row = await db.scalar(select(BaileysConnection).where(BaileysConnection.owner_id == user.id).with_for_update())
    if row is None:
        row = BaileysConnection(id=uuid.uuid4(), owner_id=user.id, agent_id=agent.id)
        db.add(row)
    elif row.enabled:
        if row.agent_id != agent.id:
            raise HTTPException(409, "Disconnect the current session before changing agents.")
        return {"started": True}
    else:
        # Leave a stopped process enough time to exit before reconnecting.
        if row.status == "disconnected" and (datetime.now(UTC) - row.updated_at).total_seconds() < 8:
            raise HTTPException(409, "Disconnect is finishing. Wait a few seconds, then reconnect.")
        if row.agent_id != agent.id:
            row.thread_id = None
        row.agent_id = agent.id
    row.enabled = True
    row.status = "starting"
    row.qr_sealed = None
    row.last_error = None
    row.heartbeat_at = None
    row.updated_at = datetime.now(UTC)
    audit.record(db, audit_context(request, user), "agent_studio.whatsapp_qr_started", target=str(row.id), details={"agent_id": str(agent.id), "mode": "self_chat"})
    await db.commit()
    return {"started": True}


@router.delete("", status_code=204)
async def disconnect(request: Request, db: DbSession, user: CurrentUser):
    row = await db.scalar(select(BaileysConnection).where(BaileysConnection.owner_id == user.id).with_for_update())
    if row:
        row.enabled = False
        row.status = "disconnected"
        row.auth_sealed = None
        row.qr_sealed = None
        row.phone_suffix = None
        row.updated_at = datetime.now(UTC)
        await db.execute(update(BaileysMessage).where(BaileysMessage.connection_id == row.id, BaileysMessage.status.in_(["queued", "ready"])).values(status="cancelled"))
        audit.record(db, audit_context(request, user), "agent_studio.whatsapp_qr_disconnected", target=str(row.id))
        await db.commit()


@router.post("/commands/{command_id}/cancel", status_code=204)
async def cancel_command(command_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    command = await db.scalar(
        select(BaileysMessage)
        .join(BaileysConnection, BaileysConnection.id == BaileysMessage.connection_id)
        .where(BaileysMessage.id == command_id, BaileysConnection.owner_id == user.id)
        .with_for_update(of=BaileysMessage)
    )
    if command is None:
        raise HTTPException(404, "WhatsApp command not found.")
    if command.status == "cancelled":
        return
    if command.status != "queued":
        raise HTTPException(409, "Only commands still in the queue can be cancelled.")
    command.status = "cancelled"
    audit.record(db, audit_context(request, user), "agent_studio.whatsapp_qr_command_cancelled", target=str(command.id))
    await db.commit()
