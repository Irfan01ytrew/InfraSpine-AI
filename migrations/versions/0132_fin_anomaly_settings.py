"""Step Y slice 5: per-department anomaly detection thresholds.

One row per department (group_id) holding the threshold overrides as JSONB.
No row means the slice-1 defaults apply. A threshold change is a settings change:
it never rewrites already-verified invoices and only affects verifications that
run after the change.

Revision ID: 0132_fin_anomaly_settings
Revises: 0103_unified_studio_governance

NOTE (merge): 0104 is also minted on sibling branches (HR leave, AI freshness,
finance autopilot, knowledge enrichment, continuity, scheduled reports, foundry,
language preference). Renumber the later revision at merge time.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0132_fin_anomaly_settings"
down_revision: str | None = "0131_self_healing_test_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fin_anomaly_settings",
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("access_groups.id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("thresholds", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("fin_anomaly_settings")
