"""Step AE slice 3 — read-only WhatsApp leads list for the business owner.

Leads belong to the WhatsApp connection owner; the list shows only the
caller's own leads (``owner_id == current user``), newest first, with an
optional ``?status=`` filter. Status changes and any CRM write-back are
later slices; this endpoint never writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.db.whatsapp_lead_models import WhatsAppLead
from app.whatsapp_auto.leads import LEAD_STATUSES, list_leads

router = APIRouter(prefix="/api/whatsapp-auto", tags=["whatsapp-auto"])


class WhatsAppLeadOut(BaseModel):
    id: uuid.UUID
    wa_id: str
    name: str | None
    phone: str | None
    email: str | None
    interest: str | None
    status: str
    source_inbound_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/leads", response_model=list[WhatsAppLeadOut])
async def list_whatsapp_leads(
    db: DbSession,
    user: CurrentUser,
    status: str | None = Query(default=None, description="Filter by lead status"),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[WhatsAppLead]:
    if status is not None and status not in LEAD_STATUSES:
        from app.core.errors import InvalidInputError

        raise InvalidInputError(f"Unknown lead status: {status!r}")
    return await list_leads(db, owner_id=user.id, status=status, limit=limit)
