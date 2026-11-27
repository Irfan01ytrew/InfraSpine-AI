"""Per-question RAG traces (metadata only) and answer feedback.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation_messages", sa.Column("feedback", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_conversation_messages_feedback_valid"), "conversation_messages", "feedback IN (-1, 1)"
    )
    op.create_table(
        "rag_traces",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("strategy", sa.String(length=40), nullable=True),
        sa.Column("decision", sa.String(length=60), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("insufficient_evidence", sa.Boolean(), nullable=True),
        sa.Column("general_knowledge", sa.Boolean(), nullable=True),
        sa.Column("question_chars", sa.Integer(), nullable=False),
        sa.Column("history_turns", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retrieved", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("timings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(length=120), nullable=True),
        sa.Column("error_stack", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'error')", name=op.f("ck_rag_traces_status_valid")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_rag_traces_user_id_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name=op.f("fk_rag_traces_conversation_id_conversations"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["conversation_messages.id"],
            name=op.f("fk_rag_traces_message_id_conversation_messages"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_traces")),
    )
    op.create_index("ix_rag_traces_created", "rag_traces", ["created_at"])
    op.create_index("ix_rag_traces_message", "rag_traces", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_rag_traces_message", table_name="rag_traces")
    op.drop_index("ix_rag_traces_created", table_name="rag_traces")
    op.drop_table("rag_traces")
    op.drop_constraint(op.f("ck_conversation_messages_feedback_valid"), "conversation_messages", type_="check")
    op.drop_column("conversation_messages", "feedback")
