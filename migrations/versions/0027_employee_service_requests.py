"""Add scoped IT/HR self-service requests and an immutable request timeline.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_to_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.String(length=12), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'submitted'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("category IN ('it', 'hr', 'access', 'facilities', 'other')", name="category_valid"),
        sa.CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="priority_valid"),
        sa.CheckConstraint(
            "status IN ('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("length(btrim(subject)) BETWEEN 1 AND 160", name="subject_length"),
        sa.CheckConstraint("length(btrim(description)) BETWEEN 1 AND 8000", name="description_length"),
        sa.CheckConstraint(
            "(status IN ('resolved', 'rejected', 'cancelled')) = (closed_at IS NOT NULL)",
            name="terminal_status_has_closed_at",
        ),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_to_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_requests_requester_created", "service_requests", ["requester_user_id", "created_at"]
    )
    op.create_index(
        "ix_service_requests_department_status",
        "service_requests",
        ["department_group_id", "status", "created_at"],
    )

    op.create_table(
        "service_request_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=True),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('submitted', 'status_changed', 'commented', 'cancelled')", name="action_valid"),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="from_status_valid",
        ),
        sa.CheckConstraint(
            "to_status IN ('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="to_status_valid",
        ),
        sa.CheckConstraint("comment IS NULL OR length(comment) <= 2000", name="comment_length"),
        sa.ForeignKeyConstraint(["request_id"], ["service_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_request_events_request_created", "service_request_events", ["request_id", "created_at"]
    )
    op.execute(
        """
        CREATE FUNCTION service_request_events_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'service request events are append-only' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER service_request_events_append_only
        BEFORE UPDATE OR DELETE ON service_request_events
        FOR EACH ROW EXECUTE FUNCTION service_request_events_block_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS service_request_events_append_only ON service_request_events")
    op.execute("DROP FUNCTION IF EXISTS service_request_events_block_mutation()")
    op.drop_index("ix_service_request_events_request_created", table_name="service_request_events")
    op.drop_table("service_request_events")
    op.drop_index("ix_service_requests_department_status", table_name="service_requests")
    op.drop_index("ix_service_requests_requester_created", table_name="service_requests")
    op.drop_table("service_requests")
