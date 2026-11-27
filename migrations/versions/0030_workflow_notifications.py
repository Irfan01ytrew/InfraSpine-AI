"""Add idempotent, recipient-scoped workflow notifications.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "notification_type IN ('run_approved', 'run_rejected', 'task_assigned')",
            name=op.f("ck_workflow_notifications_notification_type_valid"),
        ),
        sa.CheckConstraint(
            "(notification_type = 'task_assigned' AND task_id IS NOT NULL) OR "
            "(notification_type IN ('run_approved', 'run_rejected') AND task_id IS NULL)",
            name=op.f("ck_workflow_notifications_task_reference_valid"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["automation_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "recipient_user_id",
            "notification_type",
            name="uq_workflow_notifications_run_recipient_type",
        ),
    )
    op.create_index(
        "ix_workflow_notifications_recipient_created",
        "workflow_notifications",
        ["recipient_user_id", "created_at"],
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM workflow_notifications) THEN
                RAISE EXCEPTION 'export workflow notification history before downgrading migration 0030';
            END IF;
        END $$;"""
    )
    op.drop_index("ix_workflow_notifications_recipient_created", table_name="workflow_notifications")
    op.drop_table("workflow_notifications")
