"""Allow audio and video source formats for local speech transcription.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'png', 'jpeg')"
_WITH_MEDIA = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'png', 'jpeg', 'audio', 'video')"
# Earlier revisions passed an already table-qualified name through the metadata naming convention,
# producing this double-qualified constraint in existing databases. Preserve that established name.
_DOCUMENT_FORMAT_CONSTRAINT = "ck_documents_ck_documents_file_format_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(_DOCUMENT_FORMAT_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(op.f(_DOCUMENT_FORMAT_CONSTRAINT), "documents", _WITH_MEDIA)
    op.drop_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_document_versions_file_format_valid"), "document_versions", _WITH_MEDIA
    )


def downgrade() -> None:
    # Refuse to narrow the constraints while media records still exist; never delete customer data.
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM documents WHERE file_format IN ('audio', 'video'))
               OR EXISTS (SELECT 1 FROM document_versions WHERE file_format IN ('audio', 'video')) THEN
                RAISE EXCEPTION 'remove audio/video documents before downgrading migration 0020';
            END IF;
        END $$;"""
    )
    op.drop_constraint(op.f("ck_document_versions_file_format_valid"), "document_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_document_versions_file_format_valid"), "document_versions", _PREVIOUS
    )
    op.drop_constraint(op.f(_DOCUMENT_FORMAT_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(op.f(_DOCUMENT_FORMAT_CONSTRAINT), "documents", _PREVIOUS)
