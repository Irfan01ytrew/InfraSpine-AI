"""Step AB slice 2: Knowledge enrichment metadata columns on `documents`.

Derived metadata only (see `docs/step-ab-knowledge-plan.md` and
`app/rag/auto_enrich.py`): tags, an extractive summary and expiry dates
pulled from the document text. They describe the document; they never change
it and can never grant permissions or change what an approval covers.

Revision ID: 0104_knowledge_enrichment
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0104_knowledge_enrichment"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("documents", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("expiry_date", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("expiry_dates", JSONB, nullable=True))
    op.add_column("documents", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_expiry_date", "documents", ["expiry_date"])


def downgrade() -> None:
    op.drop_index("ix_documents_expiry_date", table_name="documents")
    op.drop_column("documents", "enriched_at")
    op.drop_column("documents", "expiry_dates")
    op.drop_column("documents", "expiry_date")
    op.drop_column("documents", "summary")
    op.drop_column("documents", "tags")
