"""Step L slice 3: persisted forecast records.

Revision ID: 0106_forecast_records
Revises: 0103_unified_studio_governance

Renumber note: 0104 is claimed on sibling branches (HR leave,
WhatsApp auto-reply, Knowledge freshness, continuity) and 0105 by AK,
AI freshness, and AB; 0106 is the next free number on this branch
(checked 2026-11-28: no 0106 exists in any sibling worktree).
Renumber at merge time if another branch lands 0106 first; the table
set here does not overlap with any sibling.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0106_forecast_records"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("root_id", sa.String(200), nullable=False),
        sa.Column("root_kind", sa.String(32), nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("forecast", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("drift_score", sa.Integer),
        sa.Column("created_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("horizon_days BETWEEN 1 AND 365",
                           name="ck_forecast_records_horizon_valid"),
        sa.CheckConstraint("drift_score IS NULL OR (drift_score >= 0 AND drift_score <= 100)",
                           name="ck_forecast_records_drift_valid"),
        sa.CheckConstraint("jsonb_typeof(forecast) = 'object'",
                           name="ck_forecast_records_forecast_is_object"),
        sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'",
                           name="ck_forecast_records_snapshot_is_object"),
        sa.UniqueConstraint("tenant_id", "root_id", "horizon_days", "fingerprint",
                            name="uq_forecast_records_tenant_root_horizon_fp"),
    )
    op.create_index("ix_forecast_records_root_created", "forecast_records",
                    ["tenant_id", "root_id", "horizon_days", "created_at"])
    op.create_index("ix_forecast_records_tenant_created", "forecast_records",
                    ["tenant_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM forecast_records)")):
        raise RuntimeError("Forecast records exist. Delete them (with a verified backup) instead of "
                           "downgrading, so no stored forecast silently disappears.")
    op.drop_table("forecast_records")
