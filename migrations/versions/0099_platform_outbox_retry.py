"""Outbox retry/backoff and dead-letter columns; membership source and revoke reason (module mirroring).

Revision ID: 0099_platform_outbox_retry
Revises: 0098_org_settings
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0099_platform_outbox_retry"
down_revision: str | None = "0098_org_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("platform_outbox", sa.Column("next_attempt_at", sa.DateTime(timezone=True),
                                               server_default=sa.text("now()"), nullable=False))
    op.add_column("platform_outbox", sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_platform_outbox_due", "platform_outbox", ["next_attempt_at", "id"],
                    postgresql_where=sa.text("dispatched_at IS NULL AND dead_at IS NULL"))
    op.add_column("platform_memberships", sa.Column("source", sa.String(length=64),
                                                    server_default=sa.text("'platform'"), nullable=False))
    op.add_column("platform_memberships", sa.Column("revoke_reason", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_memberships", "revoke_reason")
    op.drop_column("platform_memberships", "source")
    op.drop_index("ix_platform_outbox_due", table_name="platform_outbox")
    op.drop_column("platform_outbox", "dead_at")
    op.drop_column("platform_outbox", "next_attempt_at")
