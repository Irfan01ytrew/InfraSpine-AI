"""Step K v2 slice 2: persisted twin snapshot versions + drift history.

Stores one row per recorded twin snapshot, per graph tenant
(``twin_snapshot_versions``): fingerprint, ``as_of``, the full
snapshot JSON, the slice-1 drift report against the previous version
(``NULL`` for the first version recorded), and per-source freshness.
Versions are append-only — the same (tenant, fingerprint) pair is
never stored twice (unique constraint), and rows are never edited;
old rows only leave through the retention rule (newest 30 versions,
nothing older than 90 days, enforced by the service, not the DB).

Revision ID: 0116_twin_snapshot_versions
Revises: 0115_workforce_skills

Migration-number note: 0106 was free at the time this slice was
written — no branch claimed 0104/0105/0106/0107/0108 except the known
0104 (HR, WhatsApp AE, AI freshness, continuity, AB, AA, language,
foundry, finance autopilot, scheduled reports) and 0105 (AK, AI, AB)
collisions, which are all renumbered at merge time. If another
branch lands 0106 first, renumber this revision at merge; the table
itself is self-contained (no foreign keys into other new tables).

Downgrade drops the table. That loses version history, not the twin:
versions are derived data — the live snapshot builder can rebuild
any snapshot, and drift reports are recomputed from snapshots.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0116_twin_snapshot_versions"
down_revision: str | None = "0115_workforce_skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "twin_snapshot_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("seq", sa.BigInteger, sa.Identity(start=1, increment=1), nullable=False, unique=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column("drift_report", postgresql.JSONB, nullable=True),
        sa.Column("source_freshness", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_snapshot_versions_tenant_fingerprint"),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_snapshot_versions_fingerprint_hex"),
        sa.Index("ix_twin_snapshot_versions_tenant_created", "tenant_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("twin_snapshot_versions")
