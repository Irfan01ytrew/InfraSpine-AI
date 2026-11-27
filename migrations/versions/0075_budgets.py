"""Department budgets per period for purchase requests and expense claims.

Revision ID: 0075_budgets
Revises: 0074_approval_chains
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0075_budgets"
down_revision = "0074_approval_chains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="RESTRICT", name=op.f("fk_budgets_department_group_id_access_groups")), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("enforcement", sa.String(8), nullable=False, server_default=sa.text("'warn'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_budgets_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("category IN ('purchase', 'expense', 'all')", name=op.f("ck_budgets_category_valid")),
        sa.CheckConstraint("enforcement IN ('warn', 'block')", name=op.f("ck_budgets_enforcement_valid")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_budgets_amount_positive")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_budgets_currency_valid")),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_budgets_period_valid")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name=op.f("ck_budgets_name_valid")),
    )
    op.create_index("ix_budgets_department_period", "budgets", ["department_group_id", "period_start", "period_end"])


def downgrade() -> None:
    op.drop_index("ix_budgets_department_period", table_name="budgets")
    op.drop_table("budgets")
