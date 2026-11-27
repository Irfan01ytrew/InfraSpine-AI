"""Image documents (PNG, JPEG) and the administrative authorization to send document images to a provider.

`provider_configs.image_sharing_authorized_at/_by`: set by an admin per provider; NULL means pictures in
documents are never sent to that provider. It is cleared when a custom provider's endpoint changes.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORMATS = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'png', 'jpeg')"
_PREVIOUS_FORMATS = "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv')"


def upgrade() -> None:
    op.drop_constraint("ck_documents_file_format_valid", "documents", type_="check")
    op.create_check_constraint("ck_documents_file_format_valid", "documents", _FORMATS)
    op.add_column("provider_configs", sa.Column("image_sharing_authorized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "provider_configs",
        sa.Column(
            "image_sharing_authorized_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", name="fk_provider_configs_image_sharing_authorized_by_users", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_configs", "image_sharing_authorized_by")
    op.drop_column("provider_configs", "image_sharing_authorized_at")
    # The previous version cannot read images: remove them (their chunks cascade, and the vector index
    # reconciles against PostgreSQL on startup). Stored files become orphans on disk.
    op.execute("DELETE FROM documents WHERE file_format IN ('png', 'jpeg')")
    op.drop_constraint("ck_documents_file_format_valid", "documents", type_="check")
    op.create_check_constraint("ck_documents_file_format_valid", "documents", _PREVIOUS_FORMATS)
