"""Step AE slice 3 — CRM lead capture for WhatsApp customers.

A lead is one database row per inbound message: the contact details and
interest tags the slice-1 decider extracted, stored verbatim as data.
Capturing a lead never contacts the customer, never writes to an external
CRM, and never executes anything from the message text.

Exactly-once: the idempotency key ``whatsapp-lead:{inbound.id}`` is
unique, so a duplicate delivery (or a worker retry after a crash) inserts
nothing and returns the existing row. The insert and the read-back run in
the caller's transaction; the worker commits them with the rest of the
inbound handling.

Note on ``execute_once`` (plan §6): the plan names it for capture, but
``execute_once`` authorizes a platform duty string, and no role grants a
``crm.*`` duty — a duty-gated capture would fail closed and silently lose
the lead. The unique idempotency key enforces the same exactly-once
guarantee at the database, with a read-back of the stored row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.whatsapp_lead_models import LEAD_STATUSES, WhatsAppLead
from app.whatsapp_auto.decider import LeadSignals

__all__ = [
    "LEAD_STATUSES",
    "capture_lead",
    "lead_idempotency_key",
    "lead_values",
    "list_leads",
]


def lead_idempotency_key(inbound_id: uuid.UUID) -> str:
    """The exactly-once key for one inbound message."""
    return f"whatsapp-lead:{inbound_id}"


def lead_values(
    *,
    owner_id: uuid.UUID,
    wa_id: str,
    inbound_id: uuid.UUID,
    signals: LeadSignals,
) -> dict:
    """Build the row values for a captured lead (pure: no I/O)."""
    return {
        "id": uuid.uuid4(),
        "owner_id": owner_id,
        "wa_id": wa_id,
        "name": signals.name,
        "phone": signals.phone,
        "email": signals.email,
        "interest": ", ".join(signals.interests) if signals.interests else None,
        "source_inbound_id": inbound_id,
        "status": "new",
        "idempotency_key": lead_idempotency_key(inbound_id),
    }


async def capture_lead(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    wa_id: str,
    inbound_id: uuid.UUID,
    signals: LeadSignals,
) -> WhatsAppLead:
    """Insert one lead row for an inbound message, or return the existing
    row when this inbound was already captured (duplicate delivery)."""
    values = lead_values(owner_id=owner_id, wa_id=wa_id, inbound_id=inbound_id, signals=signals)
    inserted_id = await db.scalar(
        pg_insert(WhatsAppLead)
        .values(values)
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(WhatsAppLead.id)
    )
    if inserted_id is None:
        existing = await db.scalar(
            select(WhatsAppLead).where(WhatsAppLead.idempotency_key == values["idempotency_key"])
        )
        if existing is None:  # pragma: no cover - defensive; the key is unique
            raise RuntimeError("Lead insert conflicted but no existing row was found.")
        return existing
    row = await db.scalar(select(WhatsAppLead).where(WhatsAppLead.id == inserted_id))
    if row is None:  # pragma: no cover - defensive; just inserted
        raise RuntimeError("Lead insert returned an id with no row.")
    return row


async def list_leads(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
) -> list[WhatsAppLead]:
    """Owner-scoped lead list, newest first. Only the owner's own leads
    are ever returned; another owner's rows are invisible to the caller."""
    if status is not None and status not in LEAD_STATUSES:
        raise ValueError(f"Unknown lead status: {status!r}")
    query = (
        select(WhatsAppLead)
        .where(WhatsAppLead.owner_id == owner_id)
        .order_by(WhatsAppLead.created_at.desc(), WhatsAppLead.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    if status is not None:
        query = query.where(WhatsAppLead.status == status)
    return list(await db.scalars(query))
