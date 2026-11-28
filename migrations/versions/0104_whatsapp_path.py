"""Step F slice 3: per-owner WhatsApp transport path setting (cloud/baileys/auto).

Revision ID: 0104_whatsapp_path
Revises: 0103_unified_studio_governance

NOTE: revision number 0104 is also minted on other Step F+ branches (HR, AE,
AI freshness, AB, G, AA, AM, Z, R, AF); renumber at merge time if it
collides. The table itself is new, so a renumber is a one-line change.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_whatsapp_path"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_path_settings",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.String(length=16), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("path IN ('cloud', 'baileys', 'auto')", name=op.f("whatsapp_path_valid")),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("fk_whatsapp_path_settings_owner_id"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id", name=op.f("pk_whatsapp_path_settings")),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_path_settings")
