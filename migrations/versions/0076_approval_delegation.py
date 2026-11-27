"""Approval delegation while an approver is away, and reminders for pending approval steps.

Revision ID: 0076_approval_delegation
Revises: 0075_budgets
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0076_approval_delegation"
down_revision = "0075_budgets"
branch_labels = None
depends_on = None

_OLD_TYPES = "notification_type IN ('response_sla_escalated')"
_NEW_TYPES = "notification_type IN ('response_sla_escalated', 'approval_step_pending', 'approval_step_reminder')"


def upgrade() -> None:
    op.create_table(
        "approval_delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("delegator_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name=op.f("fk_approval_delegations_delegator_user_id_users")), nullable=False),
        sa.Column("delegate_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name=op.f("fk_approval_delegations_delegate_user_id_users")), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_approval_delegations_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("delegator_user_id <> delegate_user_id", name=op.f("ck_approval_delegations_not_self")),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f("ck_approval_delegations_period_valid")),
    )
    op.create_index("ix_approval_delegations_delegate", "approval_delegations", ["delegate_user_id", "starts_on", "ends_on"])
    op.create_index("ix_approval_delegations_delegator", "approval_delegations", ["delegator_user_id", "starts_on", "ends_on"])

    op.add_column("request_approval_steps", sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("request_approval_steps", sa.Column("reminder_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("request_approval_steps", sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("request_approval_steps", sa.Column(
        "on_behalf_of_user_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_request_approval_steps_on_behalf_of_user_id_users")), nullable=True,
    ))
    op.add_column("request_approval_steps", sa.Column("on_behalf_of_email", sa.String(320), nullable=True))
    # Steps already waiting for a decision: pending since the previous decision, or since submission.
    op.execute("""
        UPDATE request_approval_steps s SET pending_since = COALESCE(
            (SELECT max(p.decided_at) FROM request_approval_steps p WHERE p.request_id = s.request_id AND p.step_order < s.step_order),
            (SELECT r.created_at FROM service_requests r WHERE r.id = s.request_id))
        WHERE s.status = 'pending'
    """)
    op.create_index("ix_request_approval_steps_reminders", "request_approval_steps", ["pending_since"], postgresql_where=sa.text("status = 'pending'"))

    op.drop_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", type_="check")
    op.create_check_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", _NEW_TYPES)


def downgrade() -> None:
    op.execute("DELETE FROM service_request_notifications WHERE notification_type IN ('approval_step_pending', 'approval_step_reminder')")
    op.drop_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", type_="check")
    op.create_check_constraint(op.f("ck_service_request_notifications_notification_type_valid"), "service_request_notifications", _OLD_TYPES)
    op.drop_index("ix_request_approval_steps_reminders", table_name="request_approval_steps")
    for column in ("on_behalf_of_email", "on_behalf_of_user_id", "last_reminded_at", "reminder_count", "pending_since"):
        op.drop_column("request_approval_steps", column)
    op.drop_index("ix_approval_delegations_delegator", table_name="approval_delegations")
    op.drop_index("ix_approval_delegations_delegate", table_name="approval_delegations")
    op.drop_table("approval_delegations")
