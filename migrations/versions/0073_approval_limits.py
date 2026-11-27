"""Purchase requests and expense claims with amounts, and approval limits per position or person.

Revision ID: 0073_approval_limits
Revises: 0072_invitation_position
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0073_approval_limits"
down_revision = "0072_invitation_position"
branch_labels = None
depends_on = None

OLD_CATEGORIES = "category IN ('it', 'hr', 'access', 'facilities', 'other')"
NEW_CATEGORIES = "category IN ('it', 'hr', 'access', 'facilities', 'other', 'purchase', 'expense')"


def upgrade() -> None:
    op.add_column("service_requests", sa.Column("amount", sa.Numeric(16, 2), nullable=True))
    op.add_column("service_requests", sa.Column("currency", sa.String(3), nullable=True))
    op.drop_constraint(op.f("ck_service_requests_category_valid"), "service_requests", type_="check")
    op.create_check_constraint(op.f("ck_service_requests_category_valid"), "service_requests", NEW_CATEGORIES)
    op.create_check_constraint(
        op.f("ck_service_requests_amount_matches_category"),
        "service_requests",
        "(category IN ('purchase', 'expense')) = (amount IS NOT NULL AND currency IS NOT NULL) AND (amount IS NULL OR amount > 0)",
    )

    op.create_table(
        "approval_limits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("positions.id", ondelete="RESTRICT", name=op.f("fk_approval_limits_position_id_positions")), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE", name=op.f("fk_approval_limits_user_id_users")), nullable=True),
        sa.Column("max_amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_approval_limits_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("category IN ('purchase', 'expense')", name=op.f("ck_approval_limits_category_valid")),
        sa.CheckConstraint("(position_id IS NULL) <> (user_id IS NULL)", name=op.f("ck_approval_limits_one_subject")),
        sa.CheckConstraint("max_amount >= 0", name=op.f("ck_approval_limits_amount_nonnegative")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_approval_limits_currency_valid")),
    )
    op.create_index("uq_approval_limits_position", "approval_limits", ["category", "position_id"], unique=True, postgresql_where=sa.text("is_active AND position_id IS NOT NULL"))
    op.create_index("uq_approval_limits_user", "approval_limits", ["category", "user_id"], unique=True, postgresql_where=sa.text("is_active AND user_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_approval_limits_user", table_name="approval_limits")
    op.drop_index("uq_approval_limits_position", table_name="approval_limits")
    op.drop_table("approval_limits")
    # Financial requests have no place in the older schema; downgrading requires none to exist.
    op.drop_constraint(op.f("ck_service_requests_amount_matches_category"), "service_requests", type_="check")
    op.drop_constraint(op.f("ck_service_requests_category_valid"), "service_requests", type_="check")
    op.create_check_constraint(op.f("ck_service_requests_category_valid"), "service_requests", OLD_CATEGORIES)
    op.drop_column("service_requests", "currency")
    op.drop_column("service_requests", "amount")
