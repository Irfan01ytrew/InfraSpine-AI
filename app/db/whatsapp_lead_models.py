"""WhatsApp lead records (Step AE slice 3): customers who signalled buying
intent, captured as CRM rows. A lead is data only — capturing one never
contacts the customer, never writes to an external CRM, and never executes
anything from the message text.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LEAD_STATUSES = ("new", "contacted", "converted", "lost")


class WhatsAppLead(Base):
    """One captured lead per inbound WhatsApp message.

    ``owner_id`` is the business owner (the WhatsApp connection owner),
    not the customer — leads are read only by their owner. ``wa_id`` is
    the customer's WhatsApp id. ``idempotency_key`` is
    ``whatsapp-lead:{inbound.id}`` and unique, so a duplicate delivery
    can never create a second lead for the same message.
    """

    __tablename__ = "whatsapp_leads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'converted', 'lost')",
            name="whatsapp_lead_status_valid",
        ),
        UniqueConstraint("idempotency_key", name="uq_whatsapp_leads_idempotency_key"),
        UniqueConstraint("source_inbound_id", name="uq_whatsapp_leads_source_inbound"),
        Index("ix_whatsapp_leads_owner_created", "owner_id", "created_at"),
        Index("ix_whatsapp_leads_wa_id", "wa_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    wa_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    # Interest tags extracted from the message, stored verbatim as data.
    interest: Mapped[str | None] = mapped_column(Text)
    source_inbound_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_studio_whatsapp_inbound.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'new'"))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
