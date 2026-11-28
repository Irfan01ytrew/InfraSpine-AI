"""Step R slice 2: finance autopilot policy on org settings.

Autopilot on/off, dry-run mode and the two hard caps (per-payment maximum and daily
department cap) live on the single org_settings row so an administrator can change
them without touching the environment. All four default to off / unset, so existing
installations do not start sweeping on upgrade.

Revision ID: 0104_finance_autopilot
Revises: 0103_unified_studio_governance

Merge note: sibling branches also branch from 0103 (HR migration 0104 on
agent/hr-module, agent-controls 0105). Whoever merges second must resolve the
multiple-heads state with an explicit merge revision; no data is affected.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_finance_autopilot"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org_settings", sa.Column(
        "finance_autopilot_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("org_settings", sa.Column(
        "finance_autopilot_dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("org_settings", sa.Column(
        "finance_autopilot_max_payment_amount", sa.Numeric(18, 2), nullable=True))
    op.add_column("org_settings", sa.Column(
        "finance_autopilot_daily_cap", sa.Numeric(18, 2), nullable=True))
    op.create_check_constraint(
        op.f("ck_org_settings_autopilot_amounts_valid"), "org_settings",
        "finance_autopilot_max_payment_amount IS NULL OR finance_autopilot_max_payment_amount >= 0")
    op.create_check_constraint(
        op.f("ck_org_settings_autopilot_cap_valid"), "org_settings",
        "finance_autopilot_daily_cap IS NULL OR finance_autopilot_daily_cap >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_org_settings_autopilot_cap_valid"), "org_settings", type_="check")
    op.drop_constraint(op.f("ck_org_settings_autopilot_amounts_valid"), "org_settings", type_="check")
    op.drop_column("org_settings", "finance_autopilot_daily_cap")
    op.drop_column("org_settings", "finance_autopilot_max_payment_amount")
    op.drop_column("org_settings", "finance_autopilot_dry_run")
    op.drop_column("org_settings", "finance_autopilot_enabled")
