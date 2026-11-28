"""Step U slice 3: generic Idempotency-Key ledger for lightweight HTTP creates.

Revision ID: 0129_offline_idempotency_records
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0129_offline_idempotency_records"
down_revision: str | None = "0128_routing_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'completed', 'failed')", name="idempotency_status_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key_digest", name="uq_idempotency_records_user_key"),
    )
    op.create_index("ix_idempotency_records_created_at", "idempotency_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
