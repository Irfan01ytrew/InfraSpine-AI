"""Step L slice 6: persisted forecast-driven proposals.

Revision ID: 0143_forecast_proposals
Revises: 0142_workforce_budget

Renumber note: merged as 0143 (down 0142_workforce_budget) per the
integration map v9 — branch number 0107 collided with in-chain
0142_workforce_budget (J slice 7). The table set here does not
overlap with any sibling.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0143_forecast_proposals"
down_revision: str | None = "0142_workforce_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("finding_fingerprint", sa.String(64), nullable=False),
        sa.Column("finding_summary", JSONB),
        sa.Column("proposal_fingerprint", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("action", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("required_permission", sa.String(128), nullable=False),
        sa.Column("duty", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("approval_id", UUID(as_uuid=True)),
        sa.Column("execution_id", UUID(as_uuid=True)),
        sa.Column("external_ref", sa.String(500)),
        sa.Column("proposed_by", UUID(as_uuid=True), nullable=False),
        sa.Column("decided_by", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("proposal_fingerprint ~ '^[0-9a-f]{64}$'",
                           name="ck_forecast_proposals_fingerprint_hex"),
        sa.CheckConstraint("action_hash ~ '^[0-9a-f]{64}$'",
                           name="ck_forecast_proposals_action_hash_hex"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired', "
            "'cancelled', 'dispatched', 'verified', 'failed')",
            name="ck_forecast_proposals_status"),
        sa.CheckConstraint("source IN ('forecast', 'calendar')",
                           name="ck_forecast_proposals_source"),
        sa.CheckConstraint("jsonb_typeof(action) = 'object'",
                           name="ck_forecast_proposals_action_is_object"),
        sa.UniqueConstraint("tenant_id", "proposal_fingerprint",
                            name="uq_forecast_proposals_tenant_fingerprint"),
    )
    op.create_index("ix_forecast_proposals_tenant_created", "forecast_proposals",
                    ["tenant_id", "created_at"])
    op.create_index("ix_forecast_proposals_tenant_status", "forecast_proposals",
                    ["tenant_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM forecast_proposals)")):
        raise RuntimeError("Forecast proposals exist. Delete them (with a verified backup) instead of "
                           "downgrading, so no stored proposal silently disappears.")
    op.drop_table("forecast_proposals")
