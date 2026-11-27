"""Allow Markdown, JSON, HTML and WebP knowledge files.

Revision ID: 0083_document_text_formats
Revises: 0082_webhooks
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0083_document_text_formats"
down_revision: str | None = "0082_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORMATS = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'md', 'json', 'html', 'png', 'jpeg', 'webp', 'audio', 'video')"
_DOCUMENT_CONSTRAINT = "ck_documents_ck_documents_file_format_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(_DOCUMENT_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(op.f(_DOCUMENT_CONSTRAINT), "documents", _FORMATS)
    op.drop_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", type_="check")
    op.create_check_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", _FORMATS)


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM documents WHERE file_format IN ('md', 'json', 'html', 'webp'))
               OR EXISTS (SELECT 1 FROM document_versions WHERE file_format IN ('md', 'json', 'html', 'webp')) THEN
                RAISE EXCEPTION 'remove Markdown, JSON, HTML and WebP documents before downgrading migration 0083';
            END IF;
        END $$;"""
    )
    previous = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'png', 'jpeg', 'audio', 'video')"
    op.drop_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", type_="check")
    op.create_check_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", previous)
    op.drop_constraint(op.f(_DOCUMENT_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(op.f(_DOCUMENT_CONSTRAINT), "documents", previous)
