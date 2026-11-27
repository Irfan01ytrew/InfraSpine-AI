"""Add bounded retries and attempt history to QA runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0094_qa_retry_history"
down_revision = "0093_whatsapp_artifact_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "qa_runs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("qa_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "qa_runs",
        sa.Column(
            "attempt_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_qa_runs_retry_queue",
        "qa_runs",
        ["tenant_id", "owner_id", "status", "next_attempt_at", "created_at"],
    )


def downgrade():
    op.drop_index("ix_qa_runs_retry_queue", table_name="qa_runs")
    op.drop_column("qa_runs", "attempt_history")
    op.drop_column("qa_runs", "next_attempt_at")
    op.drop_column("qa_runs", "attempt_count")
