"""Step H slice 2: founder voice-note intake records + unified-runtime origin.

Revision ID: 0104_founder_voice_commands
Revises: 0103_unified_studio_governance

NOTE (merge time): revision number 0104 is also claimed by the HR leave
migration (Agent 44), WhatsApp leads (Agent 83), AI freshness (Agent 60) and
continuity unified origin (Agent 34). Renumber this revision when the Step H
branch merges; the revision *id* is unique, the *number* is not.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_founder_voice_commands"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORIGINS = "origin IN ('workflow','automation','system','studio','governance')"
NEW_ORIGINS = "origin IN ('workflow','automation','system','studio','governance','founder_voice')"


def upgrade() -> None:
    op.create_table(
        "founder_voice_commands",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("action_hash", sa.String(length=64), nullable=False),
        sa.Column("transcript_ciphertext", sa.Text(), nullable=False),
        sa.Column("transcript_nonce", sa.String(length=64), nullable=False),
        sa.Column("transcript_key_id", sa.String(length=32), nullable=False),
        sa.Column("required_permission", sa.String(length=64), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="received", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('received','mirrored','cancelled','failed')", name="status_valid"),
        sa.CheckConstraint(
            "intent IN ('finance_spend_chart','invoice_verification','continuity_replacement',"
            "'approval_request','unknown')",
            name="intent_valid",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scope_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["fin_invoice_batches.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_founder_voice_commands_action_hash", "founder_voice_commands", ["action_hash"], unique=True
    )
    op.create_index(
        "ix_founder_voice_commands_status_created", "founder_voice_commands", ["status", "created_at"]
    )
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", NEW_ORIGINS)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
                           "WHERE d.origin = 'founder_voice')")):
        raise RuntimeError("Founder voice runs exist on the unified runtime. Restore a verified backup "
                           "instead of downgrading.")
    op.drop_index("ix_founder_voice_commands_status_created", table_name="founder_voice_commands")
    op.drop_index("uq_founder_voice_commands_action_hash", table_name="founder_voice_commands")
    op.drop_table("founder_voice_commands")
    op.execute("ALTER TABLE wf_versions DISABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_versions WHERE workflow_id IN "
               "(SELECT id FROM wf_definitions WHERE origin = 'founder_voice')")
    op.execute("ALTER TABLE wf_versions ENABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_definitions WHERE origin = 'founder_voice'")
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", OLD_ORIGINS)
