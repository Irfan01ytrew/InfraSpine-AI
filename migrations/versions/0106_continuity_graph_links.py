"""Step G slice 6: graph merge links (bc_graph_links).

Revision ID: 0106_continuity_graph_links
Revises: 0105_continuity_handover_grants
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0106_continuity_graph_links"
down_revision: str | None = "0105_continuity_handover_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bc_graph_links",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("bc_tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("cg_tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("bc_node_id", postgresql.UUID(), nullable=False),
        sa.Column("cg_record_id", postgresql.UUID(), nullable=False),
        sa.Column("unified_key", sa.String(220), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bc_tenant_id"], ["bc_tenants.id"]),
        sa.ForeignKeyConstraint(["bc_tenant_id", "bc_node_id"], ["bc_nodes.tenant_id", "bc_nodes.id"],
                                name="fk_graph_links_node"),
        sa.UniqueConstraint("bc_tenant_id", "cg_tenant_id", "unified_key", name="uq_graph_links_identity"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bc_graph_links_bc_tenant_id", "bc_graph_links", ["bc_tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_bc_graph_links_bc_tenant_id", table_name="bc_graph_links")
    op.drop_table("bc_graph_links")
