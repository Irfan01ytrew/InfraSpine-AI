"""Persisted forecast records (Step L slice 3).

One row per scan forecast: the forecast itself plus the twin snapshot
it was computed from, so a later scan can score how stale the older
record has become. Records are advisory — they are written only by
the scan path (``record_scan``), never by a GET — and the newest
record per (tenant, root, horizon) is the current forecast; older
records with the same key are superseded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


#: Lifecycle statuses for forecast-driven proposals (Step L slice 6).
#: ``draft`` exists only between the row insert and the approval
#: request in the same service call; a stored row is normally already
#: ``pending_approval``.
PROPOSAL_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "dispatched",
    "verified",
    "failed",
)

#: Terminal statuses — the only ones the retention prune may delete.
TERMINAL_PROPOSAL_STATUSES = ("rejected", "expired", "cancelled", "verified", "failed")


class ForecastProposal(Base):
    """A forecast finding turned into an approval-gated action proposal.

    One row per (tenant, finding, action). The row records the exact
    canonical action (:class:`ActionSpec`, bound by its hash), who
    must approve it, the platform approval id, the ``execute_once``
    execution id once dispatched, and the lifecycle status.

    The lifecycle is: ``draft`` → ``pending_approval`` → ``approved``
    | ``rejected`` | ``expired`` | ``cancelled`` → ``dispatched`` →
    ``verified`` | ``failed``. The platform approval row is the
    source of truth for approve/reject/expire; this row mirrors it
    so the proposal inbox can be listed without joining the
    approval table. The execution row is the source of truth for
    dispatch/verify.

    A proposal never executes itself: only an approved proposal may
    be dispatched, only through ``execute_once`` with read-back
    verification (see :mod:`app.predictive_continuity.proposals_records`),
    and the executor may be neither the proposer nor the approver.
    The finding summary carries the money at risk for owner-only
    findings (redacted for non-owners at read time); the action
    params never carry money.

    Rows are never edited except for the lifecycle transitions
    (status / approval_id / execution_id / external_ref /
    decided_by). Old rows leave only through the retention rule
    (terminal proposals only — a pending or approved proposal is
    never pruned — newest 100 per tenant, nothing older than 90
    days, enforced by the service, not the DB). Dropping this table
    loses proposal history, not the forecasts: proposals are
    re-derived from the stored forecast records.
    """

    __tablename__ = "forecast_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "proposal_fingerprint",
                         name="uq_forecast_proposals_tenant_fingerprint"),
        CheckConstraint("proposal_fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_forecast_proposals_fingerprint_hex"),
        CheckConstraint("action_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_forecast_proposals_action_hash_hex"),
        CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired', "
            "'cancelled', 'dispatched', 'verified', 'failed')",
            name="ck_forecast_proposals_status",
        ),
        CheckConstraint(
            "source IN ('forecast', 'calendar')",
            name="ck_forecast_proposals_source",
        ),
        Index("ix_forecast_proposals_tenant_created", "tenant_id", "created_at"),
        Index("ix_forecast_proposals_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
        nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    finding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_permission: Mapped[str] = mapped_column(String(128), nullable=False)
    duty: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    proposed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)


class ForecastReconciliation(Base):
    """One forecast-vs-drill reconciliation report (Step L slice 7).

    One row per (tenant, drill run, forecast record): the pure
    :func:`app.predictive_continuity.reconciliation.reconcile`
    report for that pair, stored as JSONB findings + counts. The row
    is a fact about one drill run vs one stored forecast; a
    re-reconciliation of the same pair returns this row instead of
    duplicating it (unique constraint), and a re-run never edits a
    stored row — except the acknowledge fields, which only mark the
    report as seen (who + when). The forecast record, the drill run
    and the twin are never rewritten by this table.

    ``worst_severity`` is the worst finding severity (``None`` when
    the drill matched the forecast — no findings). ``adjusted_drift_score``
    is the forecast record's drift score after the miss penalty was
    folded in (``None`` when the reconciliation had no findings, so
    the score was left alone). Rows leave only through the retention
    rule (newest 100 per tenant / 90 days, enforced by the service,
    not the DB). Dropping this table loses reconciliation history,
    not the forecasts or the drills: both are re-derived by
    re-running the reconciliation.
    """

    __tablename__ = "forecast_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "drill_run_id", "forecast_record_id",
            name="uq_forecast_reconciliations_tenant_drill_record",
        ),
        CheckConstraint("forecast_fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_forecast_reconciliations_fingerprint_hex"),
        CheckConstraint("jsonb_typeof(findings) = 'array'",
                        name="ck_forecast_reconciliations_findings_is_array"),
        CheckConstraint("jsonb_typeof(counts) = 'object'",
                        name="ck_forecast_reconciliations_counts_is_object"),
        CheckConstraint(
            "worst_severity IS NULL OR worst_severity IN ('info', 'warning', 'critical')",
            name="ck_forecast_reconciliations_worst_severity",
        ),
        CheckConstraint(
            "adjusted_drift_score IS NULL OR "
            "(adjusted_drift_score >= 0 AND adjusted_drift_score <= 100)",
            name="ck_forecast_reconciliations_drift_valid",
        ),
        Index("ix_forecast_reconciliations_tenant_created", "tenant_id", "created_at"),
        Index("ix_forecast_reconciliations_tenant_drill", "tenant_id", "drill_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
        nullable=False)
    forecast_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("forecast_records.id", ondelete="CASCADE"),
        nullable=False)
    drill_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    drill_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecast_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    matches: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worst_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    findings: Mapped[dict | list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    counts: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    adjusted_drift_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)


class ForecastRecord(Base):
    """A persisted predictive-continuity forecast for one root entity."""

    __tablename__ = "forecast_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "root_id", "horizon_days", "fingerprint",
            name="uq_forecast_records_tenant_root_horizon_fp",
        ),
        Index("ix_forecast_records_root_created", "tenant_id", "root_id",
              "horizon_days", "created_at"),
        Index("ix_forecast_records_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("horizon_days BETWEEN 1 AND 365",
                        name="ck_forecast_records_horizon_valid"),
        CheckConstraint("drift_score IS NULL OR (drift_score >= 0 AND drift_score <= 100)",
                        name="ck_forecast_records_drift_valid"),
        CheckConstraint("jsonb_typeof(forecast) = 'object'",
                        name="ck_forecast_records_forecast_is_object"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'",
                        name="ck_forecast_records_snapshot_is_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
        nullable=False)
    root_id: Mapped[str] = mapped_column(String(200), nullable=False)
    root_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drift_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")
