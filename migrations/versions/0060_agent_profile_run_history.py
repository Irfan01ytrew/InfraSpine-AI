"""Persist encrypted agent run history and bounded retry state.

Revision ID: 0060_agent_profile_run_history
Revises: 0059_workspace_hierarchy
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0060_agent_profile_run_history"
down_revision = "0059_workspace_hierarchy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_profile_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "department_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("capability", sa.String(length=40), nullable=False),
        sa.Column("task_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("task_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("task_key_id", sa.String(length=32), nullable=False),
        sa.Column("result_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("result_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("result_key_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("failure_code", sa.String(length=60), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_agent_profile_runs_status_valid",
        ),
        sa.CheckConstraint("attempts BETWEEN 1 AND 2", name="ck_agent_profile_runs_attempts_valid"),
        sa.CheckConstraint(
            "capability IN ('knowledge_search', 'public_web_research', 'company_data_analysis', 'training_plan')",
            name="ck_agent_profile_runs_capability_valid",
        ),
        sa.CheckConstraint(
            "(result_ciphertext IS NULL AND result_nonce IS NULL AND result_key_id IS NULL) OR "
            "(result_ciphertext IS NOT NULL AND result_nonce IS NOT NULL AND result_key_id IS NOT NULL)",
            name="ck_agent_profile_runs_result_encryption_complete",
        ),
    )
    op.create_index(
        "ix_agent_profile_runs_requester_recent",
        "agent_profile_runs",
        ["requester_user_id", "requested_at"],
    )
    op.create_index(
        "ix_agent_profile_runs_department_recent",
        "agent_profile_runs",
        ["department_group_id", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_profile_runs_department_recent", table_name="agent_profile_runs")
    op.drop_index("ix_agent_profile_runs_requester_recent", table_name="agent_profile_runs")
    op.drop_table("agent_profile_runs")
