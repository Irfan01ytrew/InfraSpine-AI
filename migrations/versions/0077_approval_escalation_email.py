"""Escalation of overdue approval steps and an email outbox for approval notices.

Revision ID: 0077_approval_escalation_email
Revises: 0076_approval_delegation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0077_approval_escalation_email"
down_revision = "0076_approval_delegation"
branch_labels = None
depends_on = None

_OLD_TYPES = "notification_type IN ('response_sla_escalated', 'approval_step_pending', 'approval_step_reminder')"
_NEW_TYPES = "notification_type IN ('response_sla_escalated', 'approval_step_pending', 'approval_step_reminder', 'approval_step_escalated')"


def upgrade() -> None:
    op.add_column("request_approval_steps", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", type_="check")
    op.create_check_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", _NEW_TYPES)
    op.create_table(
        "approval_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("service_requests.id", ondelete="CASCADE", name=op.f("fk_approval_emails_request_id_service_requests")), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE", name=op.f("fk_approval_emails_recipient_user_id_users")), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('pending', 'reminder', 'escalation')", name=op.f("ck_approval_emails_kind_valid")),
        sa.CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'cancelled')", name=op.f("ck_approval_emails_status_valid")),
    )
    op.create_index("ix_approval_emails_due", "approval_emails", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_approval_emails_due", table_name="approval_emails")
    op.drop_table("approval_emails")
    op.execute("DELETE FROM service_request_notifications WHERE notification_type = 'approval_step_escalated'")
    op.drop_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", type_="check")
    op.create_check_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", _OLD_TYPES)
    op.drop_column("request_approval_steps", "escalated_at")
