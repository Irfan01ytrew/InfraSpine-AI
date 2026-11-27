"""Allow explicit, audited redaction of expired agent-run content.

Revision ID: 0067_agent_run_content_retention
Revises: 0066_cloud_permission_reconciliation
"""

from alembic import op
import sqlalchemy as sa


revision = "0067_agent_run_content_retention"
down_revision = "0066_cloud_permission_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_profile_runs", sa.Column("content_purged_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("agent_profile_runs", "task_ciphertext", nullable=True)
    op.alter_column("agent_profile_runs", "task_nonce", nullable=True)
    op.alter_column("agent_profile_runs", "task_key_id", nullable=True)
    op.create_check_constraint(
        "task_encryption_complete",
        "agent_profile_runs",
        "(task_ciphertext IS NULL AND task_nonce IS NULL AND task_key_id IS NULL) OR "
        "(task_ciphertext IS NOT NULL AND task_nonce IS NOT NULL AND task_key_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "content_purged_empty",
        "agent_profile_runs",
        "content_purged_at IS NULL OR (task_ciphertext IS NULL AND task_nonce IS NULL AND task_key_id IS NULL "
        "AND result_ciphertext IS NULL AND result_nonce IS NULL AND result_key_id IS NULL)",
    )
    op.create_index(
        "ix_agent_profile_runs_retention_requested_at",
        "agent_profile_runs",
        ["requested_at"],
        postgresql_where=sa.text("content_purged_at IS NULL AND status IN ('completed', 'failed')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    purged = bind.execute(
        sa.text("SELECT count(*) FROM agent_profile_runs WHERE content_purged_at IS NOT NULL")
    ).scalar_one()
    if purged:
        raise RuntimeError("Cannot remove agent-run retention metadata while redacted history exists.")
    op.drop_index("ix_agent_profile_runs_retention_requested_at", table_name="agent_profile_runs")
    op.drop_constraint("content_purged_empty", "agent_profile_runs", type_="check")
    op.drop_constraint("task_encryption_complete", "agent_profile_runs", type_="check")
    op.alter_column("agent_profile_runs", "task_key_id", nullable=False)
    op.alter_column("agent_profile_runs", "task_nonce", nullable=False)
    op.alter_column("agent_profile_runs", "task_ciphertext", nullable=False)
    op.drop_column("agent_profile_runs", "content_purged_at")
