"""Step K v2 slice 5: persisted drill-reconciliation findings.

Stores one row per unified review-queue finding, per graph tenant
(``twin_reconciliation_findings``): the drill run, the simulation
fingerprint, the finding code / resource / severity / source,
detail text (never an amount), the per-currency money difference
(redacted for non-owners at read time), the dedupe fingerprint,
and the acknowledge bookkeeping. Findings are advisory only — they
never approve, dispatch, grant, or pay anything, and acknowledging
one never changes the twin or rewrites a drill record.

Revision ID: 0118_twin_reconciliation_findings
Revises: 0117_twin_health_alerts

Migration-number note: 0109 was free at the time this slice was
written — a scan of every worktree's ``migrations/versions/``
found no 0109 file. 0108 is claimed by the mobile-app branch
(``0108_offline_idempotency_records``); 0106 is claimed by four
branches and 0107 by this branch — all renumbered at merge if
another branch lands first. If another branch lands 0109 first,
renumber this revision at merge; the table itself is
self-contained (no foreign keys into other new tables).

Downgrade drops the table. That loses review history, not the
twin: findings are re-derived by re-running the reconciliation
over the stored simulation and drill records.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0118_twin_reconciliation_findings"
down_revision: str | None = "0117_twin_health_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "twin_reconciliation_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("drill_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_kind", sa.String(64), nullable=True),
        sa.Column("simulation_fingerprint", sa.String(64), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(256), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("money_difference", postgresql.JSONB, nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("acknowledged", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_recon_findings_tenant_fingerprint"),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_recon_findings_fingerprint_hex"),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_twin_recon_findings_severity",
        ),
        sa.CheckConstraint(
            "source IN ('twin', 'continuity')",
            name="ck_twin_recon_findings_source",
        ),
        sa.Index("ix_twin_recon_findings_tenant_created", "tenant_id", "created_at"),
        sa.Index("ix_twin_recon_findings_tenant_ack", "tenant_id", "acknowledged"),
    )


def downgrade() -> None:
    op.drop_table("twin_reconciliation_findings")
