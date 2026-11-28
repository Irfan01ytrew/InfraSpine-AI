"""Step AB slice 3: daily expiry-alert check records for Knowledge documents.

One row per (document, expiry date, checked day) for documents that are
expired or due soon. The daily check is idempotent — re-running it on the
same day inserts nothing new. Rows are read-only notices: Step AA includes
pending rows in its digest; nothing here sends anything or takes any action.

Revision ID: 0109_knowledge_expiry_alerts
Revises: 0108_knowledge_enrichment

Renumbered at integration time (linear chain in INTEGRATION-SNAPSHOT.md, map v4).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0109_knowledge_expiry_alerts"
down_revision: str | None = "0108_knowledge_enrichment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_expiry_alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("urgency", sa.String(16), nullable=False),
        sa.Column("days_left", sa.Integer(), nullable=False),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("urgency IN ('expired', 'due_soon')", name="ck_knowledge_expiry_alerts_urgency_valid"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'dismissed')", name="ck_knowledge_expiry_alerts_status_valid"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "expiry_date", "checked_on", name="uq_knowledge_expiry_alerts_doc_date_day"
        ),
    )
    op.create_index(
        "ix_knowledge_expiry_alerts_checked_status", "knowledge_expiry_alerts", ["checked_on", "status"]
    )
    op.create_index("ix_knowledge_expiry_alerts_document", "knowledge_expiry_alerts", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_expiry_alerts_document", table_name="knowledge_expiry_alerts")
    op.drop_index("ix_knowledge_expiry_alerts_checked_status", table_name="knowledge_expiry_alerts")
    op.drop_table("knowledge_expiry_alerts")
