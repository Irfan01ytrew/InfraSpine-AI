"""Step J (AI Workforce OS), slice 5: per-agent budgets + org quotas.

Revision ID: 0107_workforce_budget
Revises: 0106_workforce_delegation

Note: migration number 0107 is also minted on a sibling branch (K twin
health alerts). Renumber at merge (plan §8); the table set here does
not overlap with it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0107_workforce_budget"
down_revision: str | None = "0106_workforce_delegation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_budgets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_kind", sa.String(16), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("daily_spend_limit_usd", sa.Numeric(18, 8)),
        sa.Column("monthly_spend_limit_usd", sa.Numeric(18, 8)),
        sa.Column("daily_call_limit", sa.Integer),
        sa.Column("monthly_call_limit", sa.Integer),
        sa.Column("updated_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("agent_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_budget_kind_valid"),
        sa.CheckConstraint("daily_spend_limit_usd IS NULL OR daily_spend_limit_usd >= 0",
                           name="ck_workforce_budget_daily_spend_nonnegative"),
        sa.CheckConstraint("monthly_spend_limit_usd IS NULL OR monthly_spend_limit_usd >= 0",
                           name="ck_workforce_budget_monthly_spend_nonnegative"),
        sa.CheckConstraint("daily_call_limit IS NULL OR daily_call_limit >= 0",
                           name="ck_workforce_budget_daily_calls_nonnegative"),
        sa.CheckConstraint("monthly_call_limit IS NULL OR monthly_call_limit >= 0",
                           name="ck_workforce_budget_monthly_calls_nonnegative"),
        sa.UniqueConstraint("agent_kind", "agent_id", name="uq_workforce_budgets_agent"),
        sa.Index("ix_workforce_budgets_agent", "agent_kind", "agent_id"),
    )
    op.create_table(
        "workforce_org_quotas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(32), nullable=False, server_default=sa.text("'org'")),
        sa.Column("max_agents", sa.Integer),
        sa.Column("monthly_spend_limit_usd", sa.Numeric(18, 8)),
        sa.Column("updated_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("max_agents IS NULL OR max_agents >= 0",
                           name="ck_workforce_org_quota_max_agents_nonnegative"),
        sa.CheckConstraint("monthly_spend_limit_usd IS NULL OR monthly_spend_limit_usd >= 0",
                           name="ck_workforce_org_quota_spend_nonnegative"),
        sa.UniqueConstraint("key", name="uq_workforce_org_quotas_key"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workforce_budgets) "
                           "OR EXISTS (SELECT 1 FROM workforce_org_quotas)")):
        raise RuntimeError(
            "Workforce budget rows exist. Restore a verified backup instead of downgrading.")
    op.drop_table("workforce_org_quotas")
    op.drop_table("workforce_budgets")
