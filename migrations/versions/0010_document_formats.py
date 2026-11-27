"""Multi-format documents: detected file format per document and a structured locator per chunk.

Both changes are additive. Existing documents are PDFs (`file_format` defaults to 'pdf') and existing
chunks keep `locator` NULL, which readers treat as a PDF page locator built from `page_number`.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("file_format", sa.String(length=16), server_default=sa.text("'pdf'"), nullable=False)
    )
    op.create_check_constraint(
        "ck_documents_file_format_valid", "documents", "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv')"
    )
    op.add_column("document_chunks", sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    # The previous version can only read PDFs: remove other formats (their chunks cascade, and the
    # vector index reconciles against PostgreSQL on startup). Stored files become orphans on disk.
    op.execute("DELETE FROM documents WHERE file_format <> 'pdf'")
    op.drop_column("document_chunks", "locator")
    op.drop_constraint("ck_documents_file_format_valid", "documents", type_="check")
    op.drop_column("documents", "file_format")
