"""Step G slice 5: time-limited handover grants (bc_handover_grants).

Revision ID: 0125_continuity_handover_grants
Revises: 0104_continuity_unified_origin
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0125_continuity_handover_grants"
down_revision: str | None = "0124_finance_autopilot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bc_handover_grants",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(), nullable=True),
        sa.Column("plan_id", postgresql.UUID(), nullable=True),
        sa.Column("resource_id", postgresql.UUID(), nullable=False),
        sa.Column("candidate_node_id", postgresql.UUID(), nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("scope_id", postgresql.UUID(), nullable=True),
        sa.Column("permissions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by", postgresql.UUID(), nullable=False),
        sa.Column("decided_by", postgresql.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending','active','rejected','revoked')", name="handover_grant_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["bc_tenants.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "resource_id"], ["bc_nodes.tenant_id", "bc_nodes.id"],
                                name="fk_handover_grants_resource"),
        sa.ForeignKeyConstraint(["tenant_id", "candidate_node_id"], ["bc_nodes.tenant_id", "bc_nodes.id"],
                                name="fk_handover_grants_candidate"),
        sa.ForeignKeyConstraint(["tenant_id", "scenario_id"], ["bc_scenarios.tenant_id", "bc_scenarios.id"],
                                name="fk_handover_grants_scenario"),
        sa.ForeignKeyConstraint(["tenant_id", "plan_id"], ["bc_plans.tenant_id", "bc_plans.id"],
                                name="fk_handover_grants_plan"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bc_handover_grants_tenant_id", "bc_handover_grants", ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bc_handover_grants WHERE status = 'active')")):
        raise RuntimeError("Active handover grants exist. Revoke them first instead of downgrading.")
    op.drop_index("ix_bc_handover_grants_tenant_id", table_name="bc_handover_grants")
    op.drop_table("bc_handover_grants")
