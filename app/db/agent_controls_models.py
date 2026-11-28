"""Step AK (slice 2): persistence for per-agent controls.

One control record per agent (Studio, profile, or governance kind) is the single
place that says what the agent may touch, which permissions it holds, what it
may spend, which actions it may request, and whether it is alive. The spend
ledger is append-only and is the source `spend_used_usd` is summed from, never
trusted from input. Control events are the append-only audit trail of every
control change and every denied / approval-gated decision that matters.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AGENT_CONTROL_STATUSES = ("active", "paused", "disabled", "killed")
AGENT_CONTROL_KINDS = ("studio", "profile", "governance")


class AgentControlRecord(Base):
    """The unified per-agent control record (Step AK plan §4)."""

    __tablename__ = "agent_control_records"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'disabled', 'killed')", name="ck_agent_control_status_valid"),
        CheckConstraint("agent_kind IN ('studio', 'profile', 'governance')", name="ck_agent_control_kind_valid"),
        CheckConstraint("version > 0", name="ck_agent_control_version_positive"),
        CheckConstraint("max_steps BETWEEN 1 AND 8", name="ck_agent_control_max_steps_valid"),
        CheckConstraint("max_delegation_depth >= 0", name="ck_agent_control_delegation_depth_valid"),
        CheckConstraint("spend_used_usd >= 0", name="ck_agent_control_spend_used_valid"),
        CheckConstraint(
            "spend_limit_usd IS NULL OR spend_limit_usd >= 0", name="ck_agent_control_spend_limit_valid"),
        CheckConstraint("alert_threshold > 0 AND alert_threshold <= 1",
                        name="ck_agent_control_alert_threshold_valid"),
        CheckConstraint("jsonb_typeof(data_scopes) = 'array'", name="ck_agent_control_scopes_are_array"),
        CheckConstraint("jsonb_typeof(permissions) = 'array'", name="ck_agent_control_permissions_are_array"),
        CheckConstraint("jsonb_typeof(allowed_actions) = 'array'", name="ck_agent_control_actions_are_array"),
        CheckConstraint("jsonb_typeof(delegate_to) = 'array'", name="ck_agent_control_delegates_are_array"),
        CheckConstraint("(status = 'killed' AND killed_at IS NOT NULL) OR (status <> 'killed' AND killed_at IS NULL)",
                        name="ck_agent_control_kill_details_valid"),
        UniqueConstraint("agent_id", name="uq_agent_control_records_agent_id"),
        Index("ix_agent_control_records_owner_status", "owner_id", "status"),
        Index("ix_agent_control_records_scope_status", "scope_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'studio'"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("platform_scopes.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    data_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    allowed_actions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    spend_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(19, 4))
    spend_used_usd: Mapped[Decimal] = mapped_column(
        Numeric(19, 4), nullable=False, server_default=text("0"))
    alert_threshold: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, server_default=text("0.80"))
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    delegate_to: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    max_delegation_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    killed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    killed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kill_reason: Mapped[str | None] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentControlEvent(Base):
    """Append-only audit of control changes and gated decisions (Step AK plan §4)."""

    __tablename__ = "agent_control_events"
    __table_args__ = (
        Index("ix_agent_control_events_agent_created", "agent_id", "created_at"),
        Index("ix_agent_control_events_event_created", "event", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(300), nullable=False, server_default=text("''"))
    before: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    after: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentSpendLedger(Base):
    """Append-only ledger of costed agent calls (Step AK plan §4).

    One row per costed call; `spend_used_usd` on the control record is the sum
    of this table for the agent. `idempotency_key` makes recording exactly-once:
    a retry with the same key inserts nothing and counts nothing twice.
    """

    __tablename__ = "agent_spend_ledger"
    __table_args__ = (
        CheckConstraint("tokens >= 0", name="ck_agent_spend_tokens_valid"),
        CheckConstraint("cost_usd >= 0", name="ck_agent_spend_cost_valid"),
        UniqueConstraint("idempotency_key", name="uq_agent_spend_ledger_idempotency_key"),
        Index("ix_agent_spend_ledger_agent_recorded", "agent_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    model: Mapped[str] = mapped_column(String(160), nullable=False, server_default=text("''"))
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(19, 4), nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
