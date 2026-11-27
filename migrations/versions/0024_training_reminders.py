"""Persist idempotent in-app reminders for assigned training deadlines.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reminder_type", sa.String(length=20), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reminder_type IN ('due_soon', 'overdue')", name=op.f("ck_training_reminders_reminder_type_valid")
        ),
        sa.ForeignKeyConstraint(["assignment_id"], ["training_assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id", "reminder_type", name="uq_training_reminders_assignment_type"),
    )
    op.create_index(
        "ix_training_reminders_recipient_created", "training_reminders", ["recipient_user_id", "created_at"]
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM training_reminders) THEN
                RAISE EXCEPTION 'export training reminder history before downgrading migration 0024';
            END IF;
        END $$;"""
    )
    op.drop_index("ix_training_reminders_recipient_created", table_name="training_reminders")
    op.drop_table("training_reminders")
