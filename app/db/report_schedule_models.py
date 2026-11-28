"""Scheduled reports (Step AA, slice 2): persistence for report schedules,
deliveries and per-user preferences.

A report schedule is organisation-wide and managed by administrators
(``reports.manage`` on the org scope). A delivery is one (schedule, period,
channel, recipient) send — the unique constraint is the dedupe that stops the
worker sending the same report twice. Preferences are per user and per
template; a user only ever reads or writes their own rows.

Reports are read-only summaries: nothing here executes a payment, changes an
invoice, or approves anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Time, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.reports.scheduled import CADENCES, CHANNELS, TEMPLATES

# Check-constraint literals must match app/reports/scheduled.py exactly.
_TEMPLATE_VALUES = ", ".join(f"'{t}'" for t in TEMPLATES)
_CADENCE_VALUES = ", ".join(f"'{c}'" for c in CADENCES)
_CHANNEL_VALUES = ", ".join(f"'{c}'" for c in CHANNELS)


def _uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                         onupdate=func.now())


class ReportScheduleRow(Base):
    """One recurring report delivery (organisation scope)."""

    __tablename__ = "report_schedules"
    __table_args__ = (
        CheckConstraint(f"template IN ({_TEMPLATE_VALUES})", name="template_valid"),
        CheckConstraint(f"cadence IN ({_CADENCE_VALUES})", name="cadence_valid"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday_valid"),
        CheckConstraint("overdue_days >= 0", name="overdue_days_valid"),
        CheckConstraint("quiet_start <> quiet_end", name="quiet_hours_valid"),
        Index("ix_report_schedules_due", "enabled", "claimed_until"),
    )

    id: Mapped[uuid.UUID] = _uid()
    template: Mapped[str] = mapped_column(String(32), nullable=False)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    time_of_day: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Karachi")
    # 0 = Monday ... 6 = Sunday; only used when cadence == "weekly".
    weekday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channels: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Recipient user ids, stored as strings; the worker resolves and skips inactive users.
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    quiet_start: Mapped[time] = mapped_column(Time, nullable=False, default=time(22, 0))
    quiet_end: Mapped[time] = mapped_column(Time, nullable=False, default=time(7, 0))
    overdue_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # When the most recent processed occurrence was sent (None = never).
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SKIP LOCKED claim: a worker holds the schedule until this instant.
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


# Delivery statuses: pending (queued, not yet attempted), sending (claimed by a
# worker), sent, failed (out of retries), retry (will be attempted again),
# cancelled (superseded or never applicable, e.g. template not yet implemented).
DELIVERY_STATUSES = ("pending", "sending", "sent", "failed", "retry", "cancelled")
_DELIVERY_VALUES = ", ".join(f"'{s}'" for s in DELIVERY_STATUSES)


class ReportDelivery(Base):
    """One attempted send of one report to one recipient on one channel."""

    __tablename__ = "report_deliveries"
    __table_args__ = (
        CheckConstraint(f"channel IN ({_CHANNEL_VALUES})", name="channel_valid"),
        CheckConstraint(f"status IN ({_DELIVERY_VALUES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_valid"),
        # Dedupe: the same report period is never sent twice on the same channel to the same person.
        UniqueConstraint("schedule_id", "period_start", "channel", "recipient_user_id",
                         name="uq_report_delivery_period"),
        Index("ix_report_deliveries_retry", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = _uid()
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report_schedules.id", ondelete="CASCADE"), nullable=False)
    # The scheduled occurrence this delivery belongs to (schedule-local wall time, UTC).
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending",
                                        server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    # Provider's message id for a sent WhatsApp/email delivery (the read-back
    # proof the send happened). None for in-app deliveries and older rows.
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ReportPreference(Base):
    """One user's delivery preferences for one template."""

    __tablename__ = "report_preferences"
    __table_args__ = (
        CheckConstraint(f"template IN ({_TEMPLATE_VALUES})", name="template_valid"),
        CheckConstraint(f"cadence IN ({_CADENCE_VALUES})", name="cadence_valid"),
        UniqueConstraint("user_id", "template", name="uq_report_preference_user_template"),
    )

    id: Mapped[uuid.UUID] = _uid()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    template: Mapped[str] = mapped_column(String(32), nullable=False)
    channels: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    quiet_start: Mapped[time | None] = mapped_column(Time)
    quiet_end: Mapped[time | None] = mapped_column(Time)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()

