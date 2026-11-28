"""Step J (AI Workforce OS), slice 3: the org-registry + lifecycle tables.

Revision ID: 0105_workforce_registry
Revises: 0104_workforce_skills

Note: migration number 0105 is also minted on sibling branches (AK agent
controls, AI contradiction scan, knowledge expiry alerts, scheduled
reports provider ref, continuity handover grants). Renumber at merge
(plan §8); the table set here does not overlap with any of them.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0105_workforce_registry"
down_revision: str | None = "0104_workforce_skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_registry_agents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_kind", sa.String(16), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("manager_group_id", UUID(as_uuid=True)),
        sa.Column("lifecycle_state", sa.String(16), nullable=False,
                  server_default="active"),
        sa.Column("state_reason", sa.String(500)),
        sa.Column("state_changed_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("lifecycle_state IN ('active', 'paused', 'retired')",
                           name="ck_workforce_registry_agent_state_valid"),
        sa.CheckConstraint("agent_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_registry_agent_kind_valid"),
        sa.UniqueConstraint("agent_kind", "agent_id",
                            name="uq_workforce_registry_agents_agent"),
        sa.Index("ix_workforce_registry_agents_owner", "owner_user_id"),
        sa.Index("ix_workforce_registry_agents_state", "lifecycle_state"),
    )
    op.create_table(
        "workforce_lifecycle_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_kind", sa.String(16), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("from_state", sa.String(16)),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("owner_user_id", UUID(as_uuid=True)),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("agent_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_lifecycle_event_kind_valid"),
        sa.CheckConstraint("to_state IN ('active', 'paused', 'retired')",
                           name="ck_workforce_lifecycle_event_to_state_valid"),
        sa.Index("ix_workforce_lifecycle_events_agent",
                 "agent_kind", "agent_id", "created_at"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workforce_registry_agents) "
                           "OR EXISTS (SELECT 1 FROM workforce_lifecycle_events)")):
        raise RuntimeError(
            "Workforce registry rows exist. Restore a verified backup instead of downgrading.")
    op.drop_table("workforce_lifecycle_events")
    op.drop_table("workforce_registry_agents")
