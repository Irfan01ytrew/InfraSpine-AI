"""Step AF slices 4-5: alert delivery log + per-scope alert configuration.

Revision ID: 0111_control_room_alerts
Revises: 0110_whatif_rehearsals

Renumbered at integration time (linear chain in INTEGRATION-SNAPSHOT.md, map v4).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0111_control_room_alerts"
down_revision: str | None = "0110_whatif_rehearsals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_room_alert_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("rule_key", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("severity IN ('critical','warning')", name="cr_alert_delivery_severity_valid"),
        sa.CheckConstraint("channel IN ('whatsapp','email','in_app')", name="cr_alert_delivery_channel_valid"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','skipped')",
            name="cr_alert_delivery_status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cr_alert_deliveries_scope_rule",
        "control_room_alert_deliveries",
        ["scope_id", "rule_key", "status"],
    )
    op.create_index(
        "ix_cr_alert_deliveries_scope_created",
        "control_room_alert_deliveries",
        ["scope_id", "created_at"],
    )
    op.create_table(
        "control_room_alert_configs",
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("quiet_start", sa.Time(), nullable=False, server_default=sa.text("'22:00'")),
        sa.Column("quiet_end", sa.Time(), nullable=False, server_default=sa.text("'07:00'")),
        sa.Column("recipients", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rules", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quiet_start IS NOT NULL", name="cr_alert_config_quiet_start_valid"),
        sa.CheckConstraint("quiet_end IS NOT NULL", name="cr_alert_config_quiet_end_valid"),
        sa.PrimaryKeyConstraint("scope_id"),
    )


def downgrade() -> None:
    op.drop_table("control_room_alert_configs")
    op.drop_index("ix_cr_alert_deliveries_scope_created", table_name="control_room_alert_deliveries")
    op.drop_index("ix_cr_alert_deliveries_scope_rule", table_name="control_room_alert_deliveries")
    op.drop_table("control_room_alert_deliveries")
