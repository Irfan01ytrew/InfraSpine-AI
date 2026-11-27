"""Add response deadlines and idempotent in-app service-request escalations.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("service_requests", sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("service_requests", sa.Column("response_sla_met_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("service_requests", sa.Column("sla_escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE service_requests
        SET response_due_at = created_at + CASE priority
            WHEN 'urgent' THEN interval '1 hour'
            WHEN 'high' THEN interval '4 hours'
            WHEN 'low' THEN interval '72 hours'
            ELSE interval '24 hours'
        END,
        response_sla_met_at = CASE
            WHEN status IN ('acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected')
            THEN COALESCE(closed_at, updated_at, created_at)
            ELSE NULL
        END
        """
    )
    op.alter_column("service_requests", "response_due_at", nullable=False)
    op.create_index(
        "ix_service_requests_response_sla_due",
        "service_requests",
        ["response_due_at", "id"],
        postgresql_where=sa.text(
            "status = 'submitted' AND response_sla_met_at IS NULL AND sla_escalated_at IS NULL"
        ),
    )

    op.create_table(
        "service_request_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "notification_type IN ('response_sla_escalated')",
            name="notification_type_valid",
        ),
        sa.ForeignKeyConstraint(["request_id"], ["service_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "recipient_user_id",
            "notification_type",
            name="uq_service_request_notifications_request_recipient_type",
        ),
    )
    op.create_index(
        "ix_service_request_notifications_recipient_created",
        "service_request_notifications",
        ["recipient_user_id", "created_at"],
    )

    # Migration 0027 runs with the repository's naming convention and therefore
    # creates this as ``ck_service_request_events_action_valid``. Pin the fully
    # rendered name to avoid applying that convention a second time.
    op.drop_constraint(
        op.f("ck_service_request_events_action_valid"), "service_request_events", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_service_request_events_action_valid"),
        "service_request_events",
        "action IN ('submitted', 'status_changed', 'commented', 'cancelled', 'sla_escalated')",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM service_request_notifications)
               OR EXISTS (SELECT 1 FROM service_request_events WHERE action = 'sla_escalated') THEN
                RAISE EXCEPTION 'export service request SLA notification and escalation history before downgrading migration 0029';
            END IF;
        END $$;"""
    )
    op.drop_constraint(
        op.f("ck_service_request_events_action_valid"), "service_request_events", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_service_request_events_action_valid"),
        "service_request_events",
        "action IN ('submitted', 'status_changed', 'commented', 'cancelled')",
    )
    op.drop_index(
        "ix_service_request_notifications_recipient_created", table_name="service_request_notifications"
    )
    op.drop_table("service_request_notifications")
    op.drop_index("ix_service_requests_response_sla_due", table_name="service_requests")
    op.drop_column("service_requests", "sla_escalated_at")
    op.drop_column("service_requests", "response_sla_met_at")
    op.drop_column("service_requests", "response_due_at")
