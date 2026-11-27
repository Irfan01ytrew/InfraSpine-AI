"""Tables of PDF and DOCX documents kept as rows and columns, for exact counts and totals.

Additive: a new table, written by the ingestion worker with each document's chunks. Documents indexed before
this revision have no rows here until they are reprocessed; their questions use normal retrieval as before.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_tables",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), server_default=sa.text("''"), nullable=False),
        sa.Column("locator_kind", sa.String(length=16), nullable=False),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("row_positions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary_rows", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("structured", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=200), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_document_tables_document_id_documents"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_tables")),
        sa.UniqueConstraint("document_id", "table_index", name="uq_document_tables_position"),
    )
    op.create_index(op.f("ix_document_tables_document_id"), "document_tables", ["document_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_document_tables_document_id"), table_name="document_tables")
    op.drop_table("document_tables")
