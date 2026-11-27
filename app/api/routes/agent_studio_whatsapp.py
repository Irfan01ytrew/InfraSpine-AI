"""Public Meta webhook receiver for Agent Studio WhatsApp Cloud connections."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import DbSession
from app.core.errors import InvalidInputError, NotFoundError
from app.integrations.whatsapp import accept as accept_cloud_webhook
from app.services.agent_studio_whatsapp import MAX_WEBHOOK_BYTES, accept_webhook, verify_webhook_request

router = APIRouter(prefix="/api/agent-studio/whatsapp", tags=["agent-studio-whatsapp-webhook"])


@router.get("/webhook/{connection_id}", response_class=PlainTextResponse, include_in_schema=False)
async def verify_meta_webhook(connection_id: uuid.UUID, request: Request, db: DbSession) -> PlainTextResponse:
    query = request.query_params
    if query.get("hub.mode") != "subscribe":
        raise InvalidInputError("Meta webhook mode must be subscribe.")
    token = query.get("hub.verify_token", "")
    if not await verify_webhook_request(db, connection_id, token, request.app.state.cipher):
        raise NotFoundError("WhatsApp webhook not found or verify token does not match.")
    return PlainTextResponse(query.get("hub.challenge", ""), status_code=200)


@router.post("/webhook/{connection_id}", include_in_schema=False)
async def receive_meta_webhook(connection_id: uuid.UUID, request: Request, db: DbSession) -> dict[str, bool]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_WEBHOOK_BYTES:
            raise InvalidInputError("WhatsApp webhook payload is too large.")
        raw.extend(chunk)
    signature = request.headers.get("X-Hub-Signature-256", "")
    await accept_cloud_webhook(db, connection_id, bytes(raw), signature, request.app.state.cipher)
    return {"received": True}
