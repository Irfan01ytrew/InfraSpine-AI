"""Multi-step approval chains for purchase requests and expense claims, and each request's steps.

Revision ID: 0074_approval_chains
Revises: 0073_approval_limits
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0074_approval_chains"
down_revision = "0073_approval_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_chain_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("approver_kind", sa.String(16), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("positions.id", ondelete="RESTRICT", name=op.f("fk_approval_chain_steps_position_id_positions")), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE", name=op.f("fk_approval_chain_steps_user_id_users")), nullable=True),
        sa.Column("min_amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_approval_chain_steps_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("category IN ('purchase', 'expense')", name=op.f("ck_approval_chain_steps_category_valid")),
        sa.CheckConstraint("approver_kind IN ('manager', 'position', 'person')", name=op.f("ck_approval_chain_steps_kind_valid")),
        sa.CheckConstraint(
            "(approver_kind = 'manager' AND position_id IS NULL AND user_id IS NULL)"
            " OR (approver_kind = 'position' AND position_id IS NOT NULL AND user_id IS NULL)"
            " OR (approver_kind = 'person' AND user_id IS NOT NULL AND position_id IS NULL)",
            name=op.f("ck_approval_chain_steps_approver_matches_kind"),
        ),
        sa.CheckConstraint("step_order BETWEEN 1 AND 20", name=op.f("ck_approval_chain_steps_order_valid")),
        sa.CheckConstraint("min_amount IS NULL OR min_amount >= 0", name=op.f("ck_approval_chain_steps_min_amount_nonnegative")),
        sa.CheckConstraint("length(btrim(label)) BETWEEN 2 AND 80", name=op.f("ck_approval_chain_steps_label_valid")),
    )
    op.create_index("uq_approval_chain_steps_order", "approval_chain_steps", ["category", "step_order"], unique=True, postgresql_where=sa.text("is_active"))

    op.create_table(
        "request_approval_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("service_requests.id", ondelete="CASCADE", name=op.f("fk_request_approval_steps_request_id_service_requests")), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("approver_kind", sa.String(16), nullable=False),
        sa.Column("approver_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_request_approval_steps_approver_user_id_users")), nullable=True),
        sa.Column("approver_position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("positions.id", ondelete="RESTRICT", name=op.f("fk_request_approval_steps_approver_position_id_positions")), nullable=True),
        sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_request_approval_steps_decided_by_users")), nullable=True),
        sa.Column("decided_by_email", sa.String(320), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.String(2000), nullable=True),
        sa.CheckConstraint("status IN ('waiting', 'pending', 'approved', 'rejected', 'cancelled')", name=op.f("ck_request_approval_steps_status_valid")),
        sa.CheckConstraint("approver_kind IN ('manager', 'position', 'person')", name=op.f("ck_request_approval_steps_kind_valid")),
        sa.CheckConstraint("comment IS NULL OR length(comment) <= 2000", name=op.f("ck_request_approval_steps_comment_length")),
    )
    op.create_index("uq_request_approval_steps_order", "request_approval_steps", ["request_id", "step_order"], unique=True)
    op.create_index("ix_request_approval_steps_pending", "request_approval_steps", ["status", "approver_user_id", "approver_position_id"])


def downgrade() -> None:
    op.drop_index("ix_request_approval_steps_pending", table_name="request_approval_steps")
    op.drop_index("uq_request_approval_steps_order", table_name="request_approval_steps")
    op.drop_table("request_approval_steps")
    op.drop_index("uq_approval_chain_steps_order", table_name="approval_chain_steps")
    op.drop_table("approval_chain_steps")
