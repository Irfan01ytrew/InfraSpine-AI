"""Step AE slices 2–3: WhatsApp leads table + customer-visible Knowledge subset.

- ``whatsapp_leads`` stores one CRM row per inbound customer message that
  the auto-reply decider classified as a lead (see
  ``app/db/whatsapp_lead_models.py``). ``idempotency_key`` is unique, so a
  duplicate delivery can never create a second lead for the same message.
- ``org_settings.whatsapp_customer_visible_document_ids`` is the owner
  setting that lists which Knowledge documents WhatsApp auto-replies may
  quote. Default ``[]`` (empty): auto-reply answers nothing from documents
  until the owner opts in.

Revision ID: 0135_whatsapp_leads
Revises: 0103_unified_studio_governance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0135_whatsapp_leads"
down_revision: str | None = "0134_scheduled_reports_provider_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("interest", sa.Text(), nullable=True),
        sa.Column("source_inbound_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'new'"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'contacted', 'converted', 'lost')",
            name="whatsapp_lead_status_valid",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_inbound_id"], ["agent_studio_whatsapp_inbound.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_whatsapp_leads_idempotency_key"),
        sa.UniqueConstraint("source_inbound_id", name="uq_whatsapp_leads_source_inbound"),
    )
    op.create_index(
        "ix_whatsapp_leads_owner_created", "whatsapp_leads", ["owner_id", "created_at"]
    )
    op.create_index("ix_whatsapp_leads_wa_id", "whatsapp_leads", ["wa_id"])
    op.add_column(
        "org_settings",
        sa.Column(
            "whatsapp_customer_visible_document_ids",
            JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("org_settings", "whatsapp_customer_visible_document_ids")
    op.drop_index("ix_whatsapp_leads_wa_id", table_name="whatsapp_leads")
    op.drop_index("ix_whatsapp_leads_owner_created", table_name="whatsapp_leads")
    op.drop_table("whatsapp_leads")
