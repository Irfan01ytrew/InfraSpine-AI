"""Step J (AI Workforce OS), slice 4: delegation policy + audit tables.

Revision ID: 0106_workforce_delegation
Revises: 0105_workforce_registry

Note: migration number 0106 is also minted on sibling branches (K twin
snapshot versions, M decision records, G graph links, L forecast
records, W self-healing test runs). Renumber at merge (plan §8); the
table set here does not overlap with any of them.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0106_workforce_delegation"
down_revision: str | None = "0105_workforce_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_delegation_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("rules", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("max_depth", sa.Integer, nullable=False, server_default=sa.text("2")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1",
                           name="ck_workforce_delegation_policy_version"),
        sa.CheckConstraint("max_depth BETWEEN 1 AND 5",
                           name="ck_workforce_delegation_policy_depth"),
        sa.UniqueConstraint("version", name="uq_workforce_delegation_policies_version"),
        sa.Index("ix_workforce_delegation_policies_active", "is_active"),
    )
    op.create_table(
        "workforce_delegation_audits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("parent_kind", sa.String(16), nullable=False),
        sa.Column("parent_agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("child_kind", sa.String(16), nullable=False),
        sa.Column("child_agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("parent_run_id", UUID(as_uuid=True)),
        sa.Column("child_run_id", UUID(as_uuid=True)),
        sa.Column("child_run_kind", sa.String(16), nullable=False,
                  server_default=sa.text("'inline'")),
        sa.Column("policy_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("action", sa.String(160), nullable=False, server_default=sa.text("''")),
        sa.Column("scope_id", sa.String(160), nullable=False, server_default=sa.text("''")),
        sa.Column("depth", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("parent_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_delegation_audit_parent_kind"),
        sa.CheckConstraint("child_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_delegation_audit_child_kind"),
        sa.CheckConstraint("decision IN ('allowed', 'denied')",
                           name="ck_workforce_delegation_audit_decision"),
        sa.CheckConstraint("child_run_kind IN ('inline', 'workflow')",
                           name="ck_workforce_delegation_audit_child_run_kind"),
        sa.Index("ix_workforce_delegation_audits_child",
                 "child_kind", "child_agent_id", "created_at"),
        sa.Index("ix_workforce_delegation_audits_parent_run", "parent_run_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workforce_delegation_policies) "
                           "OR EXISTS (SELECT 1 FROM workforce_delegation_audits)")):
        raise RuntimeError(
            "Workforce delegation rows exist. Restore a verified backup instead of downgrading.")
    op.drop_table("workforce_delegation_audits")
    op.drop_table("workforce_delegation_policies")
