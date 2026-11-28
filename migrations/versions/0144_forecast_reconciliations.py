"""Step L slice 7: persisted forecast-vs-drill reconciliations.

Revision ID: 0144_forecast_reconciliations
Revises: 0143_forecast_proposals

Renumber note: merged as 0144 (down 0143_forecast_proposals) per the
integration map v9 — branch number 0108 collided with in-chain
0129_offline_idempotency_records (U). The table set here does not
overlap with any sibling.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0144_forecast_reconciliations"
down_revision: str | None = "0143_forecast_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_reconciliations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("forecast_record_id", UUID(as_uuid=True),
                  sa.ForeignKey("forecast_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("drill_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("drill_kind", sa.String(64)),
        sa.Column("forecast_fingerprint", sa.String(64), nullable=False),
        sa.Column("matches", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("worst_severity", sa.String(16)),
        sa.Column("findings", JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("counts", JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("adjusted_drift_score", sa.Integer),
        sa.Column("acknowledged", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("forecast_fingerprint ~ '^[0-9a-f]{64}$'",
                           name="ck_forecast_reconciliations_fingerprint_hex"),
        sa.CheckConstraint("jsonb_typeof(findings) = 'array'",
                           name="ck_forecast_reconciliations_findings_is_array"),
        sa.CheckConstraint("jsonb_typeof(counts) = 'object'",
                           name="ck_forecast_reconciliations_counts_is_object"),
        sa.CheckConstraint(
            "worst_severity IS NULL OR worst_severity IN ('info', 'warning', 'critical')",
            name="ck_forecast_reconciliations_worst_severity"),
        sa.CheckConstraint(
            "adjusted_drift_score IS NULL OR "
            "(adjusted_drift_score >= 0 AND adjusted_drift_score <= 100)",
            name="ck_forecast_reconciliations_drift_valid"),
        sa.UniqueConstraint("tenant_id", "drill_run_id", "forecast_record_id",
                            name="uq_forecast_reconciliations_tenant_drill_record"),
    )
    op.create_index("ix_forecast_reconciliations_tenant_created", "forecast_reconciliations",
                    ["tenant_id", "created_at"])
    op.create_index("ix_forecast_reconciliations_tenant_drill", "forecast_reconciliations",
                    ["tenant_id", "drill_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM forecast_reconciliations)")):
        raise RuntimeError("Forecast reconciliations exist. Delete them (with a verified backup) instead of "
                           "downgrading, so no stored reconciliation silently disappears.")
    op.drop_table("forecast_reconciliations")
