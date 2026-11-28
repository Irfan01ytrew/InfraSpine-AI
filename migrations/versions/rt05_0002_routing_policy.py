"""Step T slice 4: stored routing policy table for the smart model router.

Revision ID: rt05_0002_routing_policy
Revises: rt05_0001_model_routing

One singleton row (id = 1, like org_settings) holds the admin-editable
routing policy: default policy, per-task-class overrides, per-model
capability tiers, the org-wide monthly budget and per-department budgets.
No credentials, keys or endpoints are stored here. At merge time this
revision must be rebased onto the merge-time head if rt05_0001 is no
longer the head (parallel branches already use 0104/0105).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "rt05_0002_routing_policy"
down_revision: str | None = "rt05_0001_model_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routing_policy_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("default_policy", sa.String(length=16), server_default=sa.text("'cheapest'"), nullable=False),
        sa.Column("task_class_policies", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("capability_tiers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("monthly_budget_usd", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("department_budgets", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("id = 1", name="single_row"),
        sa.CheckConstraint(
            "default_policy IN ('cheapest', 'fastest', 'balanced')", name="default_policy_valid"
        ),
        sa.CheckConstraint(
            "monthly_budget_usd IS NULL OR monthly_budget_usd >= 0",
            name="monthly_budget_non_negative",
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=op.f("fk_routing_policy_settings_updated_by_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_policy_settings")),
    )


def downgrade() -> None:
    op.drop_table("routing_policy_settings")
