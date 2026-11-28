"""Persisted drill-reconciliation findings (Step K v2 slice 5).

One row per unified review-queue finding, per graph tenant. A
finding records that one twin simulation disagreed with one real
Step G drill — either on the twin side (affected set, money totals,
recovery waves) or on the Step G side (RTO / MTPD / RPO timing).
Findings are advisory only: a row never approves, dispatches,
grants access, or pays anything, and acknowledging a finding only
marks the queue row as seen — it never changes the twin, never
rewrites a drill record, and never auto-applies anything.

The dedupe fingerprint is ``(tenant, drill run, simulation
fingerprint, code, resource, money)`` (see
:func:`app.digital_twin.reconciliation.finding_fingerprint`), so
re-reconciling the same drill run against the same simulation
stores each finding once; a new simulation of the same drill run
produces new rows. Money differences live in the ``money_difference``
JSONB field (per-currency exposure diffs, never account numbers)
and are redacted for non-owners at read time; the ``detail`` text
never contains an amount.

Rows are never edited except for the acknowledge bookkeeping
(``acknowledged`` / ``acknowledged_at`` / ``acknowledged_by``);
old rows only leave through the retention rule (newest 100 findings
per tenant, nothing older than 90 days, enforced by the service,
not the DB). Dropping this table loses review history, not the
twin: findings are re-derived by re-running the reconciliation
over the stored simulation and drill records.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TwinReconciliationFinding(Base):
    __tablename__ = "twin_reconciliation_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_recon_findings_tenant_fingerprint"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_recon_findings_fingerprint_hex"),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_twin_recon_findings_severity",
        ),
        CheckConstraint(
            "source IN ('twin', 'continuity')",
            name="ck_twin_recon_findings_source",
        ),
        Index("ix_twin_recon_findings_tenant_created", "tenant_id", "created_at"),
        Index("ix_twin_recon_findings_tenant_ack", "tenant_id", "acknowledged"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    drill_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scenario_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    money_difference: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["TwinReconciliationFinding"]
