"""Add idempotent due-date reminders for personal assignments.

Revision ID: 0044_personal_task_reminders
Revises: 0043_guided_knowledge_interviews
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0044_personal_task_reminders"
down_revision = "0043_guided_knowledge_interviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_task_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reminder_type", sa.String(length=20), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reminder_type IN ('due_soon', 'overdue')", name=op.f("ck_personal_task_reminders_reminder_type_valid")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_personal_task_reminders_recipient_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["personal_tasks.id"], ondelete="RESTRICT", name=op.f("fk_personal_task_reminders_task_id_personal_tasks")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personal_task_reminders")),
        sa.UniqueConstraint("task_id", "reminder_type", name="uq_personal_task_reminders_task_type"),
    )
    op.create_index(
        "ix_personal_task_reminders_recipient_created",
        "personal_task_reminders",
        ["recipient_user_id", "created_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM personal_task_reminders) THEN
                RAISE EXCEPTION 'export personal task reminder history before downgrading migration 0044';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_personal_task_reminders_recipient_created", table_name="personal_task_reminders")
    op.drop_table("personal_task_reminders")
