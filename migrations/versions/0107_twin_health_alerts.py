"""Step K v2 slice 3: persisted twin health alerts.

Stores one row per fired health-watch alert, per graph tenant
(``twin_health_alerts``): rule key, severity, dedupe fingerprint
(rule + tenant + twin version), health/drift scores, trust, title
and detail. Alerts are advisory only — the detail carries score
points and counts, never money amounts or entity names.

Revision ID: 0107_twin_health_alerts
Revises: 0106_twin_snapshot_versions

Migration-number note: 0107 was free at the time this slice was
written — a scan of every worktree's ``migrations/versions/`` found
no 0107 file (0106 is claimed by four branches: twin snapshot
versions, forecast records, decision records, continuity graph
links — all renumbered at merge). If another branch lands 0107
first, renumber this revision at merge; the table itself is
self-contained (no foreign keys into other new tables).

Downgrade drops the table. That loses alert history, not the twin:
alerts are re-derived by re-evaluating the watch over the stored
twin versions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0107_twin_health_alerts"
down_revision: str | None = "0106_twin_snapshot_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "twin_health_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_key", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("health_score", sa.Integer, nullable=False),
        sa.Column("drift_score", sa.Integer, nullable=True),
        sa.Column("trust", sa.String(16), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_health_alerts_tenant_fingerprint"),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_health_alerts_fingerprint_hex"),
        sa.CheckConstraint("health_score >= 0 AND health_score <= 100", name="ck_twin_health_alerts_score_range"),
        sa.Index("ix_twin_health_alerts_tenant_created", "tenant_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("twin_health_alerts")
