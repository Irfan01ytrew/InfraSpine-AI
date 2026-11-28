"""Step O (voice-first founder mode), slices 2-3: voice proposals on the unified runtime.

Adds the `voice` origin to `wf_definitions` and the `voice_proposals` table that
carries a voice proposal from intake (route) through approval (platform approval
request) to execution (unified workflow run, `origin='voice'`).

Revision ID: 0104_voice_origin
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0104_voice_origin"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORIGINS = "origin IN ('workflow','automation','system','studio','governance')"
NEW_ORIGINS = "origin IN ('workflow','automation','system','studio','governance','voice')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", NEW_ORIGINS)
    op.create_table(
        "voice_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("intent_kind", sa.String(32), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("action", JSONB, nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("transcript_excerpt", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
        sa.Column("wf_run_id", UUID(as_uuid=True), sa.ForeignKey("wf_runs.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_result", JSONB, nullable=True),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('proposed','approved','rejected','expired','executed','failed')",
                           name="ck_voice_proposals_status_valid"),
        sa.Index("ix_voice_proposals_group", "group_id", "created_at"),
        sa.Index("ix_voice_proposals_approval", "approval_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
                           "WHERE d.origin = 'voice')")):
        raise RuntimeError("Voice runs exist on the unified runtime. Restore a verified backup "
                           "instead of downgrading.")
    op.execute("ALTER TABLE wf_versions DISABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_versions WHERE workflow_id IN "
               "(SELECT id FROM wf_definitions WHERE origin = 'voice')")
    op.execute("ALTER TABLE wf_versions ENABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_definitions WHERE origin = 'voice'")
    op.drop_table("voice_proposals")
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", OLD_ORIGINS)
