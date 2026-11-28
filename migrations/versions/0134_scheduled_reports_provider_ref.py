"""Step AA slice 4: provider reference on report deliveries.

Revision ID: 0134_scheduled_reports_provider_ref
Revises: 0104_scheduled_reports

The delivery log records the provider's message id for every sent WhatsApp
or email delivery (the read-back proof that the send happened). The column
is nullable: in-app deliveries have no provider, and older rows predate it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0134_scheduled_reports_provider_ref"
down_revision: str | None = "0133_scheduled_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report_deliveries", sa.Column("provider_ref", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("report_deliveries", "provider_ref")
