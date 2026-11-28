"""Step K v2 slice 6: persisted twin-driven proposals.

Stores one row per proposal, per graph tenant
(``twin_proposals``): the source finding, the exact canonical
action (:class:`ActionSpec`, bound by its hash), who must approve
it, the platform approval id, the ``execute_once`` execution id
once dispatched, and the lifecycle status. A proposal never
executes itself — only an approved proposal may be dispatched,
only through ``execute_once`` with read-back verification, and
the executor may be neither the proposer nor the approver.

Revision ID: 0119_twin_proposals
Revises: 0118_twin_reconciliation_findings

Migration-number note: 0110 was free at the time this slice was
written — a scan of every worktree's ``migrations/versions/``
found no 0110 file. 0109 is this branch (slice 5), 0108 is the
mobile-app branch (``0108_offline_idempotency_records``); 0106
is claimed by four branches and 0107 by this branch — all
renumbered at merge if another branch lands first. If another
branch lands 0110 first, renumber this revision at merge; the
table itself is self-contained (no foreign keys into other new
tables).

Downgrade drops the table. That loses proposal history, not the
twin: proposals are re-derived from the stored findings.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0119_twin_proposals"
down_revision: str | None = "0118_twin_reconciliation_findings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "twin_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("finding_fingerprint", sa.String(64), nullable=False),
        sa.Column("finding_summary", postgresql.JSONB, nullable=True),
        sa.Column("proposal_fingerprint", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("action", postgresql.JSONB, nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("required_permission", sa.String(128), nullable=False),
        sa.Column("duty", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_ref", sa.String(500), nullable=True),
        sa.Column("proposed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "proposal_fingerprint", name="uq_twin_proposals_tenant_fingerprint"),
        sa.CheckConstraint("proposal_fingerprint ~ '^[0-9a-f]{64}$'",
                           name="ck_twin_proposals_fingerprint_hex"),
        sa.CheckConstraint("action_hash ~ '^[0-9a-f]{64}$'", name="ck_twin_proposals_action_hash_hex"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired', "
            "'cancelled', 'dispatched', 'verified', 'failed')",
            name="ck_twin_proposals_status",
        ),
        sa.CheckConstraint(
            "source IN ('reconciliation', 'simulation')",
            name="ck_twin_proposals_source",
        ),
        sa.Index("ix_twin_proposals_tenant_created", "tenant_id", "created_at"),
        sa.Index("ix_twin_proposals_tenant_status", "tenant_id", "status"),
    )


def downgrade() -> None:
    op.drop_table("twin_proposals")
