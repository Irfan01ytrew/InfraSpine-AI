"""Persist fenced WhatsApp report and image delivery."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0093_whatsapp_artifact_delivery"
down_revision = "0092_whatsapp_progress_receipt"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "integration_whatsapp_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("inbound_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_whatsapp_inbound.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_key", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("media_id", sa.String(length=160), nullable=True),
        sa.Column("provider_message_id", sa.String(length=160), nullable=True),
        sa.Column("delivery_status", sa.String(length=24), nullable=True),
        sa.Column("last_error", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("inbound_id", "artifact_key", name="uq_integration_whatsapp_artifact_key"),
        sa.UniqueConstraint("provider_message_id", name="uq_integration_whatsapp_artifact_message_id"),
        sa.CheckConstraint("status IN ('queued', 'uploading', 'uploaded', 'sending', 'accepted', 'failed', 'uncertain')", name="ck_integration_whatsapp_artifact_status"),
    )
    op.create_index("ix_integration_whatsapp_artifacts_inbound", "integration_whatsapp_artifacts", ["inbound_id", "status"])


def downgrade():
    op.drop_index("ix_integration_whatsapp_artifacts_inbound", table_name="integration_whatsapp_artifacts")
    op.drop_table("integration_whatsapp_artifacts")
