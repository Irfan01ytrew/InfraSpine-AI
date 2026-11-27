"""Bind custom-provider credentials to the endpoint origin they were verified against.

Adds `provider_credentials.endpoint_origin`. Existing custom-provider credentials are re-encrypted with
an origin-bound associated data by the application at startup (`rebind_legacy_credentials`), because
re-encryption needs the master key, which migrations deliberately never receive.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_credentials", sa.Column("endpoint_origin", sa.String(length=300), nullable=True))


def downgrade() -> None:
    # Origin-bound ciphertexts cannot be decrypted by the previous version. Shred them (as retirement
    # does) instead of leaving unusable secrets behind; administrators re-enter those keys.
    op.execute(
        "UPDATE provider_credentials SET status = 'retired', retired_at = now(), ciphertext = NULL, nonce = NULL "
        "WHERE endpoint_origin IS NOT NULL AND status <> 'retired'"
    )
    op.drop_column("provider_credentials", "endpoint_origin")
