"""Step AF slices 4-5 persistence: alert delivery log + per-scope alert configuration.

The delivery log is the durable memory that makes cooldown/dedupe survive a
worker restart (plan §8 slice 4): every alert the evaluator fires is recorded
here before it is sent, so a second evaluation pass — or a second worker —
never sends the same alert twice inside its cooldown window.

The config table holds one row per workflow scope (department). Rules are
data, not code: thresholds, cooldowns, quiet hours and recipients are
configured per scope and merged over the slice-1 ``DEFAULT_RULES`` by
``app.control_room.config.effective_rules``. No recipient is ever taken from
document or message text — only the admin-configured list in this table is
used (plan §3).
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, Text, Time, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ControlRoomAlertDelivery(Base):
    """One alert send attempt for one scope, rule and recipient channel.

    ``status`` lifecycle: ``pending`` (queued by the evaluator) →
    ``sending`` (claimed by a worker with SKIP LOCKED) → ``sent`` or
    ``failed`` (``error`` holds the failure, truncated). ``skipped`` is for
    rows the worker decided not to send (e.g. no channel could be reached —
    the alert is still visible on the dashboard as in-app). ``recipient`` is
    NULL for the ``in_app`` channel. Alert text carries metric values only,
    never account numbers, tokens or keys.
    """

    __tablename__ = "control_room_alert_deliveries"
    __table_args__ = (
        CheckConstraint("severity IN ('critical','warning')", name="cr_alert_delivery_severity_valid"),
        CheckConstraint("channel IN ('whatsapp','email','in_app')", name="cr_alert_delivery_channel_valid"),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','skipped')",
            name="cr_alert_delivery_status_valid",
        ),
        Index("ix_cr_alert_deliveries_scope_rule", "scope_id", "rule_key", "status"),
        Index("ix_cr_alert_deliveries_scope_created", "scope_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uid()
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default=text("'pending'"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ControlRoomAlertConfig(Base):
    """Per-scope alert configuration (one row per workflow scope).

    ``recipients`` is a list of ``{"channel": "whatsapp"|"email", "address": ...}``
    objects — the only addresses alerts are ever sent to. ``rules`` maps a
    rule key to overrides (``enabled``, ``threshold``, ``cooldown_seconds``,
    ``breaks_quiet_hours``, ``channels``); any key not listed keeps its
    slice-1 default. ``quiet_start``/``quiet_end`` are org-local times
    (Asia/Karachi); a missing row means defaults everywhere.
    """

    __tablename__ = "control_room_alert_configs"
    __table_args__ = (
        CheckConstraint("quiet_start IS NOT NULL", name="cr_alert_config_quiet_start_valid"),
        CheckConstraint("quiet_end IS NOT NULL", name="cr_alert_config_quiet_end_valid"),
    )

    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    quiet_start: Mapped[time] = mapped_column(Time, nullable=False, default=time(22, 0), server_default=text("'22:00'"))
    quiet_end: Mapped[time] = mapped_column(Time, nullable=False, default=time(7, 0), server_default=text("'07:00'"))
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
