"""Store provider ACLs separately from the local department boundary.

Revision ID: 0063_cloud_source_acl_intersection
Revises: 0062_approved_department_messages
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0063_cloud_source_acl_intersection"
down_revision = "0062_approved_department_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_source_acl",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "principal_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(principal_user_id, principal_group_id) = 1", name="ck_cloud_source_acl_one_principal"
        ),
        sa.UniqueConstraint(
            "document_id",
            "principal_user_id",
            "principal_group_id",
            name="uq_cloud_source_acl_principal",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_cloud_source_acl_user", "cloud_source_acl", ["principal_user_id"])
    op.create_index("ix_cloud_source_acl_group", "cloud_source_acl", ["principal_group_id"])


def downgrade() -> None:
    op.drop_index("ix_cloud_source_acl_group", table_name="cloud_source_acl")
    op.drop_index("ix_cloud_source_acl_user", table_name="cloud_source_acl")
    op.drop_table("cloud_source_acl")
