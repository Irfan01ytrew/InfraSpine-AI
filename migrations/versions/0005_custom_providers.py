"""Allow admins to add isolated OpenAI-compatible providers from the UI.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_configs", sa.Column("display_name", sa.String(length=80), nullable=True))
    op.add_column("provider_configs", sa.Column("base_url", sa.String(length=500), nullable=True))
    op.create_check_constraint(
        "ck_provider_configs_custom_provider_fields_together",
        "provider_configs",
        "(base_url IS NULL AND display_name IS NULL) OR "
        "(base_url IS NOT NULL AND display_name IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_provider_configs_custom_provider_fields_together", "provider_configs", type_="check"
    )
    op.drop_column("provider_configs", "base_url")
    op.drop_column("provider_configs", "display_name")
