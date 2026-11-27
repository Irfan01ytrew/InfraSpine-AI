"""Organisation settings with the business time zone (default Asia/Karachi) used for business days.

Revision ID: 0098_org_settings
Revises: 0097_platform_core
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098_org_settings"
down_revision: str | None = "0097_platform_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('org_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('time_zone', sa.String(length=64), server_default=sa.text("'Asia/Karachi'"), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'PKR'"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_org_settings_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_org_settings'))
    )
    op.execute("INSERT INTO org_settings (id) VALUES (1) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    op.drop_table("org_settings")
