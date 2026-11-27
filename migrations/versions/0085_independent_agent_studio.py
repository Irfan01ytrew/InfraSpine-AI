"""Create the independent, owner-scoped LangGraph Agent Studio.

Revision ID: 0085_independent_agent_studio
Revises: 0084_immutable_conversation_messages
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0085_independent_agent_studio"
down_revision: str | None = "0084_immutable_conversation_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_studio_agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), server_default=sa.text("''"), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tool_permissions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("approval_tools", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("delegate_agent_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("rag_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_steps", sa.Integer(), server_default=sa.text("6"), nullable=False),
        sa.Column("provider_id", sa.String(64)),
        sa.Column("model", sa.String(160)),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_agent_studio_agents_version_positive"),
        sa.CheckConstraint("max_steps BETWEEN 1 AND 8", name="ck_agent_studio_agents_max_steps_valid"),
        sa.CheckConstraint("jsonb_typeof(tool_permissions) = 'array'", name="ck_agent_studio_agents_tools_are_array"),
        sa.CheckConstraint("jsonb_typeof(approval_tools) = 'array'", name="ck_agent_studio_agents_approval_tools_are_array"),
    )
    op.create_index("ix_agent_studio_agents_owner_updated", "agent_studio_agents", ["owner_id", "updated_at"])

    op.create_table(
        "agent_studio_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(180), server_default=sa.text("'New agent run'"), nullable=False),
        sa.Column("history_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("history_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("history_key_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_studio_threads_owner_updated", "agent_studio_threads", ["owner_id", "updated_at"])
    op.create_index("ix_agent_studio_threads_agent_updated", "agent_studio_threads", ["agent_id", "updated_at"])

    op.create_table(
        "agent_studio_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'running'"), nullable=False),
        sa.Column("state_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("state_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("state_key_id", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('running', 'awaiting_approval', 'completed', 'failed')", name="ck_agent_studio_runs_status_valid"),
    )
    op.create_index("ix_agent_studio_runs_owner_created", "agent_studio_runs", ["owner_id", "created_at"])
    op.create_index("ix_agent_studio_runs_thread_created", "agent_studio_runs", ["thread_id", "created_at"])
    op.create_index("ix_agent_studio_runs_status_created", "agent_studio_runs", ["status", "created_at"])

    op.create_table(
        "agent_studio_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("action_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("action_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("action_key_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decision_note", sa.String(500)),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_agent_studio_approvals_status_valid"),
    )
    op.create_index("ix_agent_studio_approvals_owner_status", "agent_studio_approvals", ["owner_id", "status"])
    op.create_index("ix_agent_studio_approvals_run", "agent_studio_approvals", ["run_id"])

    op.create_table(
        "agent_studio_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("memory_key", sa.String(100), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("value_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("value_key_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "owner_id", "memory_key", name="uq_agent_studio_memory_key"),
    )
    op.create_index("ix_agent_studio_memories_owner_agent", "agent_studio_memories", ["owner_id", "agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_studio_memories_owner_agent", table_name="agent_studio_memories")
    op.drop_table("agent_studio_memories")
    op.drop_index("ix_agent_studio_approvals_run", table_name="agent_studio_approvals")
    op.drop_index("ix_agent_studio_approvals_owner_status", table_name="agent_studio_approvals")
    op.drop_table("agent_studio_approvals")
    op.drop_index("ix_agent_studio_runs_status_created", table_name="agent_studio_runs")
    op.drop_index("ix_agent_studio_runs_thread_created", table_name="agent_studio_runs")
    op.drop_index("ix_agent_studio_runs_owner_created", table_name="agent_studio_runs")
    op.drop_table("agent_studio_runs")
    op.drop_index("ix_agent_studio_threads_agent_updated", table_name="agent_studio_threads")
    op.drop_index("ix_agent_studio_threads_owner_updated", table_name="agent_studio_threads")
    op.drop_table("agent_studio_threads")
    op.drop_index("ix_agent_studio_agents_owner_updated", table_name="agent_studio_agents")
    op.drop_table("agent_studio_agents")
