"""Retain archived source files for document version history.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_documents_sha256", "documents", ["sha256"])
    op.create_index("ix_documents_source_uri", "documents", ["source_uri"])
    op.create_table(
        "document_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_format", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=24), server_default=sa.text("'replacement'"), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_document_versions_version_positive")),
        sa.CheckConstraint(
            "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'png', 'jpeg')",
            name=op.f("ck_document_versions_file_format_valid"),
        ),
        sa.ForeignKeyConstraint(["archived_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version", name=op.f("uq_document_versions_document_version")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_document_versions_storage_key")),
    )
    op.create_index(
        "ix_document_versions_document_archived", "document_versions", ["document_id", "archived_at"]
    )


def downgrade() -> None:
    # Archived files are retained by the application. A downgrade only removes the metadata table.
    op.drop_index("ix_document_versions_document_archived", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_source_uri", table_name="documents")
    op.drop_index("ix_documents_sha256", table_name="documents")
