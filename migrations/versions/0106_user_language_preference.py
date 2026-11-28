"""Step Z slice 3: per-user language preference.

Revision ID: 0106_user_language_preference
Revises: 0105_foundry_registry

Renumbered at integration time (was 0104_user_language_preference on
agent/multilingual; the linear chain is recorded in INTEGRATION-SNAPSHOT.md).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0106_user_language_preference"
down_revision: str | None = "0105_foundry_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_language_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("language", sa.String(length=16), server_default=sa.text("'en'"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("language IN ('en', 'roman_urdu', 'urdu')", name="language_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_language_preferences")
