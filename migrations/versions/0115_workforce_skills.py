"""Step J (AI Workforce OS), slice 2: the skill registry tables.

Revision ID: 0115_workforce_skills
Revises: 0114_founder_voice_commands

Note: migration number 0104 is also minted on sibling branches (HR leave,
WhatsApp AE leads, AI freshness metadata, knowledge enrichment, finance
autopilot, continuity unification, scheduled reports, foundry registry).
Renumber at merge (plan §8); the table set here does not overlap with any
of them.

Renumbered at integration time (linear chain in INTEGRATION-SNAPSHOT.md, map v4).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0115_workforce_skills"
down_revision: str | None = "0114_founder_voice_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_skills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("category", sa.String(60)),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("char_length(name) >= 2 AND char_length(name) <= 60",
                           name="ck_workforce_skill_name_length"),
        sa.UniqueConstraint("name", name="uq_workforce_skills_name"),
        sa.Index("ix_workforce_skills_category", "category"),
    )
    op.create_table(
        "workforce_agent_skills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_kind", sa.String(16), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", UUID(as_uuid=True),
                  sa.ForeignKey("workforce_skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verified_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("agent_kind IN ('studio', 'governance', 'profile')",
                           name="ck_workforce_agent_skill_kind_valid"),
        sa.CheckConstraint(
            "(verified_at IS NULL AND verified_by_user_id IS NULL AND expires_at IS NULL) OR "
            "(verified_at IS NOT NULL AND verified_by_user_id IS NOT NULL)",
            name="ck_workforce_agent_skill_verification_atomic"),
        sa.CheckConstraint(
            "expires_at IS NULL OR verified_at IS NULL OR expires_at > verified_at",
            name="ck_workforce_agent_skill_expiry_after_verification"),
        sa.UniqueConstraint("agent_kind", "agent_id", "skill_id",
                            name="uq_workforce_agent_skills_agent_skill"),
        sa.Index("ix_workforce_agent_skills_agent", "agent_kind", "agent_id"),
        sa.Index("ix_workforce_agent_skills_skill", "skill_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workforce_agent_skills) "
                           "OR EXISTS (SELECT 1 FROM workforce_skills)")):
        raise RuntimeError(
            "Workforce skill rows exist. Restore a verified backup instead of downgrading.")
    op.drop_table("workforce_agent_skills")
    op.drop_table("workforce_skills")
