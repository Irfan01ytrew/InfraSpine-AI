"""Personal memory (ADR-004 WP2): per-user preferences, terminology and personal facts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=100), nullable=True),
        sa.Column("subject_key", sa.String(length=100), nullable=True),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column("content_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_conversation_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('preference', 'terminology', 'fact')", name=op.f("ck_memory_items_kind_valid")),
        sa.CheckConstraint("status IN ('active', 'superseded')", name=op.f("ck_memory_items_status_valid")),
        sa.CheckConstraint("source IN ('chat', 'manual')", name=op.f("ck_memory_items_source_valid")),
        sa.CheckConstraint("length(btrim(content)) BETWEEN 1 AND 500", name=op.f("ck_memory_items_content_length")),
        sa.CheckConstraint("(subject IS NULL) = (subject_key IS NULL)", name=op.f("ck_memory_items_subject_has_key")),
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_at IS NOT NULL)", name=op.f("ck_memory_items_superseded_has_time")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_memory_items_owner_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["memory_items.id"], name=op.f("fk_memory_items_supersedes_id_memory_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"], ["conversations.id"],
            name=op.f("fk_memory_items_source_conversation_id_conversations"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(
        "uq_memory_items_active_content", "memory_items", ["owner_user_id", "content_key"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_memory_items_active_subject", "memory_items", ["owner_user_id", "subject_key"],
        unique=True, postgresql_where=sa.text("status = 'active' AND subject_key IS NOT NULL"),
    )
    op.create_index("ix_memory_items_owner_recent", "memory_items", ["owner_user_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_table("memory_items")
