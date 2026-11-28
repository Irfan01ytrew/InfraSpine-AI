"""Step AA slice 2: persistence for scheduled reports — report_schedules,
report_deliveries and report_preferences tables.

Revision ID: 0133_scheduled_reports
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0133_scheduled_reports"
down_revision: str | None = "0132_fin_anomaly_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = ("daily_cash_summary", "weekly_invoices", "overdue_alert")
CADENCES = ("daily", "weekly")
CHANNELS = ("whatsapp", "email", "in_app")
DELIVERY_STATUSES = ("pending", "sending", "sent", "failed", "retry", "cancelled")


def _template_check(table: str) -> str:
    values = ", ".join(f"'{t}'" for t in TEMPLATES)
    return f"template IN ({values})"


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("template", sa.String(32), nullable=False),
        sa.Column("cadence", sa.String(16), nullable=False),
        sa.Column("time_of_day", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Karachi"),
        sa.Column("weekday", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("recipients", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("quiet_start", sa.Time(), nullable=False, server_default="22:00:00"),
        sa.Column("quiet_end", sa.Time(), nullable=False, server_default="07:00:00"),
        sa.Column("overdue_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(_template_check("report_schedules"), name="template_valid"),
        sa.CheckConstraint("cadence IN ('daily', 'weekly')", name="cadence_valid"),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday_valid"),
        sa.CheckConstraint("overdue_days >= 0", name="overdue_days_valid"),
        sa.CheckConstraint("quiet_start <> quiet_end", name="quiet_hours_valid"),
    )
    op.create_index("ix_report_schedules_due", "report_schedules", ["enabled", "claimed_until"])

    op.create_table(
        "report_deliveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", UUID(as_uuid=True),
                  sa.ForeignKey("report_schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("recipient_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("channel IN ('whatsapp', 'email', 'in_app')", name="channel_valid"),
        sa.CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed', 'retry', 'cancelled')",
                           name="status_valid"),
        sa.CheckConstraint("attempts >= 0", name="attempts_valid"),
        sa.UniqueConstraint("schedule_id", "period_start", "channel", "recipient_user_id",
                            name="uq_report_delivery_period"),
    )
    op.create_index("ix_report_deliveries_retry", "report_deliveries", ["status", "next_attempt_at"])

    op.create_table(
        "report_preferences",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("template", sa.String(32), nullable=False),
        sa.Column("channels", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cadence", sa.String(16), nullable=False, server_default="daily"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("quiet_start", sa.Time()),
        sa.Column("quiet_end", sa.Time()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(_template_check("report_preferences"), name="template_valid"),
        sa.CheckConstraint("cadence IN ('daily', 'weekly')", name="cadence_valid"),
        sa.UniqueConstraint("user_id", "template", name="uq_report_preference_user_template"),
    )


def downgrade() -> None:
    op.drop_table("report_preferences")
    op.drop_table("report_deliveries")
    op.drop_table("report_schedules")
