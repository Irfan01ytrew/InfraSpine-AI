"""Add client-project workspace hierarchy, ownership and lifecycle administration.

Revision ID: 0059_workspace_hierarchy
Revises: 0058_agent_action_approval
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0059_workspace_hierarchy"
down_revision = "0058_agent_action_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "access_groups",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "access_groups",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "access_groups",
        sa.Column("lifecycle_status", sa.String(24), server_default="active", nullable=False),
    )
    op.create_check_constraint(
        "ck_access_groups_lifecycle_status_valid",
        "access_groups",
        "lifecycle_status IN ('active', 'archived')",
    )
    op.create_index(
        "ix_access_groups_parent_id",
        "access_groups",
        ["parent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_access_groups_parent_id", table_name="access_groups")
    op.drop_constraint("ck_access_groups_lifecycle_status_valid", "access_groups", type_="check")
    op.drop_column("access_groups", "lifecycle_status")
    op.drop_column("access_groups", "owner_id")
    op.drop_column("access_groups", "parent_id")
