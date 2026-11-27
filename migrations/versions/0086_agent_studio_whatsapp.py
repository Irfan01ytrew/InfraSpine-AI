"""Add secure WhatsApp Cloud API integration for Agent Studio.

Revision ID: 0086_agent_studio_whatsapp
Revises: 0085_independent_agent_studio
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0086_agent_studio_whatsapp"
down_revision: str | None = "0085_independent_agent_studio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_studio_whatsapp_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone_number_id", sa.String(64), nullable=False),
        sa.Column("business_account_id", sa.String(64), nullable=False),
        sa.Column("allowed_senders", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("api_version", sa.String(12), server_default=sa.text("'v26.0'"), nullable=False),
        sa.Column("access_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("access_token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("access_token_key_id", sa.String(32), nullable=False),
        sa.Column("app_secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("app_secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("app_secret_key_id", sa.String(32), nullable=False),
        sa.Column("verify_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("verify_token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("verify_token_key_id", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("verified_name", sa.String(200)),
        sa.Column("display_phone_number", sa.String(40)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", name="uq_agent_studio_whatsapp_owner"),
        sa.CheckConstraint("api_version ~ '^v[0-9]+\\.0$'", name="ck_agent_studio_whatsapp_api_version_valid"),
    )
    op.create_index("ix_agent_studio_whatsapp_phone", "agent_studio_whatsapp_connections", ["phone_number_id"])

    op.create_table(
        "agent_studio_whatsapp_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_whatsapp_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wa_id", sa.String(32), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_threads.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("connection_id", "wa_id", name="uq_agent_studio_whatsapp_contact"),
    )
    op.create_index("ix_agent_studio_whatsapp_contacts_owner", "agent_studio_whatsapp_contacts", ["owner_id", "updated_at"])

    op.create_table(
        "agent_studio_whatsapp_inbound",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_studio_whatsapp_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_message_id", sa.String(160), nullable=False, unique=True),
        sa.Column("from_wa_id", sa.String(32), nullable=False),
        sa.Column("message_type", sa.String(24), server_default=sa.text("'text'"), nullable=False),
        sa.Column("text_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("text_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("text_key_id", sa.String(32), nullable=False),
        sa.Column("reply_ciphertext", sa.LargeBinary()),
        sa.Column("reply_nonce", sa.LargeBinary()),
        sa.Column("reply_key_id", sa.String(32)),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'processing', 'completed', 'failed')", name="ck_agent_studio_whatsapp_inbound_status_valid"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_agent_studio_whatsapp_inbound_attempts_valid"),
    )
    op.create_index("ix_agent_studio_whatsapp_inbound_queue", "agent_studio_whatsapp_inbound", ["status", "next_attempt_at", "created_at"])
    op.create_index("ix_agent_studio_whatsapp_inbound_owner", "agent_studio_whatsapp_inbound", ["owner_id", "created_at"])


def downgrade() -> None:
    # Databases created from the application model before 0091 can hold this dependent table without a
    # migration that owns it; remove it first so the inbound table below can be dropped.
    op.execute("DROP TABLE IF EXISTS integration_whatsapp_dispatches")
    op.drop_index("ix_agent_studio_whatsapp_inbound_owner", table_name="agent_studio_whatsapp_inbound")
    op.drop_index("ix_agent_studio_whatsapp_inbound_queue", table_name="agent_studio_whatsapp_inbound")
    op.drop_table("agent_studio_whatsapp_inbound")
    op.drop_index("ix_agent_studio_whatsapp_contacts_owner", table_name="agent_studio_whatsapp_contacts")
    op.drop_table("agent_studio_whatsapp_contacts")
    op.drop_index("ix_agent_studio_whatsapp_phone", table_name="agent_studio_whatsapp_connections")
    op.drop_table("agent_studio_whatsapp_connections")
