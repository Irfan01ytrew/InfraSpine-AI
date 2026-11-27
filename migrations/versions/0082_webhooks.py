"""Outgoing webhooks and their delivery log.

Revision ID: 0082_webhooks
Revises: 0081_request_form_audience
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0082_webhooks"
down_revision = "0081_request_form_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("secret_key_id", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("disabled_reason", sa.String(200), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_webhooks_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name=op.f("ck_webhooks_name_valid")),
        sa.CheckConstraint("jsonb_typeof(events) = 'array'", name=op.f("ck_webhooks_events_array")),
        sa.CheckConstraint("consecutive_failures >= 0", name=op.f("ck_webhooks_failures_nonnegative")),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("webhook_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("webhooks.id", ondelete="CASCADE", name=op.f("fk_webhook_deliveries_webhook_id_webhooks")), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'cancelled')", name=op.f("ck_webhook_deliveries_status_valid")),
    )
    op.create_index("ix_webhook_deliveries_due", "webhook_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_webhook_deliveries_webhook_created", "webhook_deliveries", ["webhook_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_webhook_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
