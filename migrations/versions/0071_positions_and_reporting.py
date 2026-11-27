"""Positions (job titles), each person's position and manager, and the history of those assignments.

Revision ID: 0071_positions_and_reporting
Revises: 0070_invitation_custom_role
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0071_positions_and_reporting"
down_revision = "0070_invitation_custom_role"
branch_labels = None
depends_on = None

JOB_FAMILIES = (
    "executive", "management", "professional", "technical", "operations",
    "administration", "governance", "sales", "frontline", "external", "other",
)


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("job_family", sa.String(24), nullable=False, server_default=sa.text("'other'")),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_positions_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 2 AND 120", name=op.f("ck_positions_title_valid")),
        sa.CheckConstraint("job_family IN (" + ", ".join(f"'{f}'" for f in JOB_FAMILIES) + ")", name=op.f("ck_positions_job_family_valid")),
        sa.CheckConstraint("level IS NULL OR level BETWEEN 1 AND 20", name=op.f("ck_positions_level_valid")),
    )
    op.create_index("uq_positions_active_title", "positions", [sa.text("lower(title)")], unique=True, postgresql_where=sa.text("is_active"))

    op.add_column("users", sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("positions.id", ondelete="RESTRICT", name=op.f("fk_users_position_id_positions")), nullable=True))
    op.add_column("users", sa.Column("reports_to_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_users_reports_to_id_users")), nullable=True))
    op.create_index("ix_users_reports_to_id", "users", ["reports_to_id"])

    op.create_table(
        "org_assignment_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE", name=op.f("fk_org_assignment_history_user_id_users")), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("positions.id", ondelete="RESTRICT", name=op.f("fk_org_assignment_history_position_id_positions")), nullable=True),
        sa.Column("position_title", sa.String(120), nullable=True),
        sa.Column("reports_to_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_org_assignment_history_reports_to_id_users")), nullable=True),
        sa.Column("reports_to_email", sa.String(320), nullable=True),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_org_assignment_history_changed_by_users")), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_org_assignment_history_user", "org_assignment_history", ["user_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_org_assignment_history_user", table_name="org_assignment_history")
    op.drop_table("org_assignment_history")
    op.drop_index("ix_users_reports_to_id", table_name="users")
    op.drop_column("users", "reports_to_id")
    op.drop_column("users", "position_id")
    op.drop_index("uq_positions_active_title", table_name="positions")
    op.drop_table("positions")
