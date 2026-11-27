"""Persist immutable evidence-backed what-if calculation scenarios.

Revision ID: 0041_forecasts
Revises: 0040_folder_sync_monitor
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0041_forecasts"
down_revision = "0040_folder_sync_monitor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("metric", sa.String(100), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("period_unit", sa.String(16), nullable=False),
        sa.Column("horizon_periods", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("observations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assumptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("projections", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 160", name="ck_forecast_scenarios_title_valid"),
        sa.CheckConstraint("length(btrim(metric)) BETWEEN 1 AND 100", name="ck_forecast_scenarios_metric_valid"),
        sa.CheckConstraint("length(btrim(unit)) BETWEEN 1 AND 32", name="ck_forecast_scenarios_unit_valid"),
        sa.CheckConstraint("period_unit IN ('period', 'month', 'quarter', 'year')", name="ck_forecast_scenarios_period_unit_valid"),
        sa.CheckConstraint("horizon_periods BETWEEN 1 AND 36", name="ck_forecast_scenarios_horizon_valid"),
        sa.CheckConstraint("model_version = 'linear-growth-v1'", name="ck_forecast_scenarios_model_version_valid"),
        sa.CheckConstraint("length(input_digest) = 64", name="ck_forecast_scenarios_digest_valid"),
        sa.CheckConstraint("jsonb_typeof(observations) = 'array' AND jsonb_array_length(observations) BETWEEN 2 AND 24", name="ck_forecast_scenarios_observations_valid"),
        sa.CheckConstraint("jsonb_typeof(assumptions) = 'object'", name="ck_forecast_scenarios_assumptions_valid"),
        sa.CheckConstraint("jsonb_typeof(projections) = 'array' AND jsonb_array_length(projections) BETWEEN 1 AND 36", name="ck_forecast_scenarios_projections_valid"),
    )
    op.create_index("ix_forecast_scenarios_owner_created", "forecast_scenarios", ["owner_user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_forecast_scenarios_owner_created", table_name="forecast_scenarios")
    op.drop_table("forecast_scenarios")
