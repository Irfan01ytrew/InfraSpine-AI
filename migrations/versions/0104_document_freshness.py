"""Step AI slice 2: owner-set freshness metadata on documents.

`review_date` is when the content should next be reviewed, `expires_at` is when it
stops being valid, and `superseded_by` points at the document that replaces this
one. All three are owner-set metadata: they are never derived from document text
(a document can never mark itself fresh), and they are only ever set by an owner
action — ingestion never writes them.

Revision ID: 0104_document_freshness
Revises: 0103_unified_studio_governance
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0104_document_freshness"
down_revision = "0103_unified_studio_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("review_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_documents_superseded_by", "documents", ["superseded_by"])
    op.create_check_constraint(
        "ck_documents_superseded_by_not_self",
        "documents",
        "superseded_by IS NULL OR superseded_by <> id",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM documents
                       WHERE review_date IS NOT NULL OR expires_at IS NOT NULL OR superseded_by IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot downgrade while document freshness metadata exists';
            END IF;
        END $$;"""
    )
    op.drop_constraint("ck_documents_superseded_by_not_self", "documents", type_="check")
    op.drop_index("ix_documents_superseded_by", table_name="documents")
    op.drop_column("documents", "superseded_by")
    op.drop_column("documents", "expires_at")
    op.drop_column("documents", "review_date")
