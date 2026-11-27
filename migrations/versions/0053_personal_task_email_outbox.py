"""Add a retryable SMTP outbox for personal task deadline reminders.

Revision ID: 0053_personal_task_email_outbox
Revises: 0052_user_totp_mfa
"""

from alembic import op
import sqlalchemy as sa


revision = "0053_personal_task_email_outbox"
down_revision = "0052_user_totp_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personal_task_reminders",
        sa.Column("email_status", sa.String(length=16), server_default="disabled", nullable=False),
    )
    op.add_column(
        "personal_task_reminders",
        sa.Column("email_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("personal_task_reminders", sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "personal_task_reminders", sa.Column("email_next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "personal_task_reminders", sa.Column("email_claimed_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "personal_task_reminders", sa.Column("email_last_error_code", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_personal_task_reminders_email_status_valid"),
        "personal_task_reminders",
        "email_status IN ('disabled', 'pending', 'sending', 'sent', 'retry', 'failed', 'cancelled')",
    )
    op.create_index(
        "ix_personal_task_reminders_email_queue",
        "personal_task_reminders",
        ["email_status", "email_next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_personal_task_reminders_email_queue", table_name="personal_task_reminders")
    op.drop_constraint(op.f("ck_personal_task_reminders_email_status_valid"), "personal_task_reminders", type_="check")
    op.drop_column("personal_task_reminders", "email_last_error_code")
    op.drop_column("personal_task_reminders", "email_claimed_until")
    op.drop_column("personal_task_reminders", "email_next_attempt_at")
    op.drop_column("personal_task_reminders", "email_sent_at")
    op.drop_column("personal_task_reminders", "email_attempts")
    op.drop_column("personal_task_reminders", "email_status")
