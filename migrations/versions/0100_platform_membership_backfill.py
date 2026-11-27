"""Backfill platform scopes and memberships from existing users, groups and module tenants.

Revision ID: 0100_platform_membership_backfill
Revises: 0099_platform_outbox_retry

Idempotent: safe to re-run. Downgrade removes only rows created by this mirror (source legacy:* and
scopes with a legacy_table), leaving platform-granted memberships and the organisation scope in place.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0100_platform_membership_backfill"
down_revision: str | None = "0099_platform_outbox_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from app.platform.sync import backfill_all

    backfill_all(op.get_bind())


def downgrade() -> None:
    # Memberships mirrored from legacy tables, then the scopes that existed only as a legacy mirror.
    # The organisation scope (no legacy_table) and any platform-granted memberships stay.
    op.execute("DELETE FROM platform_memberships WHERE source LIKE 'legacy:%'")
    op.execute("DELETE FROM platform_scopes WHERE legacy_table IS NOT NULL")
