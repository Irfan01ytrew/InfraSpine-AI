"""Track quoted document obligations and immutable assignment history.

Revision ID: 0045_document_obligations
Revises: 0044_proposal_coverage
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045_document_obligations"
down_revision: str | None = "0044_proposal_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_obligations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.Column("source_page_number", sa.Integer(), nullable=False),
        sa.Column("source_locator", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("assigned_to_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("'1'"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_document_obligations_title_length"),
        sa.CheckConstraint("length(btrim(source_quote)) BETWEEN 8 AND 1200", name="ck_document_obligations_source_quote_length"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_document_obligations_rationale_length"),
        sa.CheckConstraint("source_version >= 1", name="ck_document_obligations_source_version_positive"),
        sa.CheckConstraint("source_page_number >= 1", name="ck_document_obligations_source_page_positive"),
        sa.CheckConstraint("version >= 1", name="ck_document_obligations_version_positive"),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'completed')", name="ck_document_obligations_status_valid"),
        sa.CheckConstraint("length(source_sha256) = 64", name="ck_document_obligations_source_hash_length"),
        sa.CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="ck_document_obligations_completion_timestamp_valid"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_to_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_obligations_assignee_status", "document_obligations", ["assigned_to_user_id", "status", "due_at"])
    op.create_index("ix_document_obligations_document_status", "document_obligations", ["document_id", "status", "due_at"])
    op.create_table(
        "document_obligation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("obligation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("rationale", sa.String(length=2000), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "action IN ('created', 'assigned', 'reassigned', 'due_date_changed', 'acknowledged', 'completed', 'reopened')",
            name="ck_document_obligation_events_action_valid",
        ),
        sa.CheckConstraint("from_status IS NULL OR from_status IN ('open', 'acknowledged', 'completed')", name="ck_document_obligation_events_from_status_valid"),
        sa.CheckConstraint("to_status IS NULL OR to_status IN ('open', 'acknowledged', 'completed')", name="ck_document_obligation_events_to_status_valid"),
        sa.CheckConstraint("rationale IS NULL OR length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_document_obligation_events_rationale_length"),
        sa.ForeignKeyConstraint(["obligation_id"], ["document_obligations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_obligation_events_obligation", "document_obligation_events", ["obligation_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_document_obligation_events_obligation", table_name="document_obligation_events")
    op.drop_table("document_obligation_events")
    op.drop_index("ix_document_obligations_document_status", table_name="document_obligations")
    op.drop_index("ix_document_obligations_assignee_status", table_name="document_obligations")
    op.drop_table("document_obligations")
