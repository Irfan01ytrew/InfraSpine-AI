"""Step J (AI Workforce OS), slice 5: persistence for budget enforcement.

Two tables:

* ``workforce_budgets`` — one row per workforce agent that has explicit
  limits: daily / monthly spend caps (USD) and daily / monthly call
  caps. Every limit is nullable: ``NULL`` means "no limit set" (the
  per-agent default monthly spend cap of USD 10 from plan §7.1 still
  applies — see ``app.workforce.budget``). Agents without a row use the
  defaults. Limits are money, never invented prices: spend is the
  recorded cost of runs already done.
* ``workforce_org_quotas`` — a single row (``key = 'org'``): the org-wide
  caps — max agents in the registry and total monthly workforce spend.
  Both nullable: unset means no org cap (plan §7.5 — per-agent limits
  still apply).

A budget row does not FK to the island tables — the islands are three
separate tables — so the service resolves the agent at write time and
fails closed when the agent does not exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkforceBudget(Base):
    """Explicit per-agent spend / call limits (defaults apply without a row)."""

    __tablename__ = "workforce_budgets"
    __table_args__ = (
        CheckConstraint(
            "agent_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_budget_kind_valid",
        ),
        CheckConstraint(
            "daily_spend_limit_usd IS NULL OR daily_spend_limit_usd >= 0",
            name="ck_workforce_budget_daily_spend_nonnegative",
        ),
        CheckConstraint(
            "monthly_spend_limit_usd IS NULL OR monthly_spend_limit_usd >= 0",
            name="ck_workforce_budget_monthly_spend_nonnegative",
        ),
        CheckConstraint(
            "daily_call_limit IS NULL OR daily_call_limit >= 0",
            name="ck_workforce_budget_daily_calls_nonnegative",
        ),
        CheckConstraint(
            "monthly_call_limit IS NULL OR monthly_call_limit >= 0",
            name="ck_workforce_budget_monthly_calls_nonnegative",
        ),
        UniqueConstraint("agent_kind", "agent_id", name="uq_workforce_budgets_agent"),
        Index("ix_workforce_budgets_agent", "agent_kind", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    daily_spend_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    monthly_spend_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    daily_call_limit: Mapped[int | None] = mapped_column()
    monthly_call_limit: Mapped[int | None] = mapped_column()
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkforceOrgQuota(Base):
    """The org-wide quotas: max agents and total monthly spend (one row)."""

    __tablename__ = "workforce_org_quotas"
    __table_args__ = (
        CheckConstraint(
            "max_agents IS NULL OR max_agents >= 0",
            name="ck_workforce_org_quota_max_agents_nonnegative",
        ),
        CheckConstraint(
            "monthly_spend_limit_usd IS NULL OR monthly_spend_limit_usd >= 0",
            name="ck_workforce_org_quota_spend_nonnegative",
        ),
        UniqueConstraint("key", name="uq_workforce_org_quotas_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'org'"))
    max_agents: Mapped[int | None] = mapped_column()
    monthly_spend_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
