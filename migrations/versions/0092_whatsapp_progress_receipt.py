"""Persist Cloud API progress-message delivery receipts."""

from alembic import op
import sqlalchemy as sa


revision = "0092_whatsapp_progress_receipt"
down_revision = "0091_agent_studio_durable_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("integration_whatsapp_dispatches", sa.Column("progress_message_id", sa.String(length=160), nullable=True))
    op.add_column("integration_whatsapp_dispatches", sa.Column("progress_status", sa.String(length=24), nullable=True))
    op.add_column("integration_whatsapp_dispatches", sa.Column("progress_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_integration_whatsapp_dispatch_progress_id",
        "integration_whatsapp_dispatches",
        ["progress_message_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_integration_whatsapp_dispatch_progress_id", table_name="integration_whatsapp_dispatches")
    op.drop_column("integration_whatsapp_dispatches", "progress_at")
    op.drop_column("integration_whatsapp_dispatches", "progress_status")
    op.drop_column("integration_whatsapp_dispatches", "progress_message_id")
