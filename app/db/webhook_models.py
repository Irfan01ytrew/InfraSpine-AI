"""Outgoing webhooks: administrator-registered HTTPS endpoints notified of request and approval events."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

WEBHOOK_EVENTS = (
    "request.submitted", "request.status_changed", "request.approved", "request.rejected",
    "request.cancelled", "approval.step_pending",
)


class Webhook(Base):
    __tablename__ = "webhooks"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name="name_valid"),
        CheckConstraint("jsonb_typeof(events) = 'array'", name="events_array"),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    events: Mapped[list] = mapped_column(JSONB, nullable=False)
    # The signing secret, encrypted at rest and bound to this row; shown to the administrator only once.
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    consecutive_failures: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    disabled_reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'cancelled')", name="status_valid"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_webhook_created", "webhook_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(200))
    duration_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
