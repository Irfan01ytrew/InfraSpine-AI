"""Create direct, scoped personal assignments for My Work.

Revision ID: 0042_personal_tasks
Revises: 0041_forecasts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0042_personal_tasks"
down_revision = "0041_forecasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("body", sa.String(length=4000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'completed', 'cancelled')", name="ck_personal_tasks_status_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_personal_tasks_title_length"),
        sa.CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="ck_personal_tasks_body_length"),
        sa.CheckConstraint(
            "(status = 'open' AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND completed_at IS NULL AND cancelled_at IS NOT NULL)",
            name="ck_personal_tasks_terminal_timestamp_valid",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_personal_tasks"),
    )
    op.create_index(
        "ix_personal_tasks_assignee_deadline", "personal_tasks", ["assignee_user_id", "status", "due_at"]
    )
    op.create_index(
        "ix_personal_tasks_creator_created", "personal_tasks", ["created_by_user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_personal_tasks_creator_created", table_name="personal_tasks")
    op.drop_index("ix_personal_tasks_assignee_deadline", table_name="personal_tasks")
    op.drop_table("personal_tasks")
