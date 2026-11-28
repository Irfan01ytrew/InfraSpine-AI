"""Drill-reconciliation review queue service (Step L slice 7).

Async service over the stored reconciliation reports. Records the
report of one reconciliation
(:func:`app.predictive_continuity.reconciliation.reconcile`), lists
the review queue, counts it, acknowledges reports, and prunes old
ones. The scheduled entry point is
:func:`evaluate_reconciliation`: the worker/CLI calls it with a
stored forecast record and the Step G drill comparison for one
drill run.

No endpoint and no service function here approves, dispatches,
grants access, or pays anything, and nothing here ever rewrites a
drill record. The one write besides the queue row itself is the
miss penalty: when a reconciliation finds the forecast was wrong,
the linked forecast record's stored drift score is raised (never
lowered) through
:func:`app.predictive_continuity.reconciliation.apply_miss_penalty`
— the forecast content is untouched, only its staleness signal.
Acknowledging a report only marks the queue row as seen (who +
when) — the report itself stays in the queue until retention
prunes it. Money at risk on findings is redacted for non-owners
at read time (see :func:`redact_money_for_viewer`); the finding
``detail`` text never contains an amount in the first place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select

from app.predictive_continuity import reconciliation
from app.predictive_continuity.models import ForecastReconciliation, ForecastRecord

#: Reconciliation retention defaults (same 90-day age rule as
#: forecast records; 100 reports is the per-tenant cap — reports
#: are small rows and a queue shorter than the forecast history
#: would hide repeat disagreements).
DEFAULT_RECONCILIATION_RETENTION_COUNT = 100
DEFAULT_RECONCILIATION_RETENTION_DAYS = 90

#: Hard cap on list responses so the review-queue list stays bounded.
MAX_LIST_LIMIT = 100

__all__ = [
    "DEFAULT_RECONCILIATION_RETENTION_COUNT",
    "DEFAULT_RECONCILIATION_RETENTION_DAYS",
    "MAX_LIST_LIMIT",
    "acknowledge_reconciliation",
    "evaluate_reconciliation",
    "get_reconciliation",
    "list_reconciliations",
    "prune_reconciliations",
    "reconciliation_counts",
    "reconciliation_summary_row",
    "record_reconciliation",
    "redact_money_for_viewer",
]


def reconciliation_summary_row(row):
    """Small read-only summary of a stored reconciliation report.

    ``findings`` carry per-process money at risk for the owner;
    non-owners get the redacted copy (see
    :func:`redact_money_for_viewer`). The ``detail`` text itself
    never contains an amount.
    """
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "forecast_record_id": str(row.forecast_record_id),
        "drill_run_id": str(row.drill_run_id),
        "drill_kind": row.drill_kind,
        "forecast_fingerprint": row.forecast_fingerprint,
        "matches": row.matches,
        "worst_severity": row.worst_severity,
        "findings": row.findings,
        "counts": row.counts,
        "adjusted_drift_score": row.adjusted_drift_score,
        "money_redacted": False,
        "acknowledged": row.acknowledged,
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_by": str(row.acknowledged_by) if row.acknowledged_by else None,
        "created_at": row.created_at.isoformat(),
    }


def redact_money_for_viewer(summary):
    """Copy of a reconciliation summary with finding money hidden.

    ``None`` in → ``None`` out. The owner-only rule (plan §4):
    exposure amounts stay owner-only; the finding codes,
    severities, and details are safe for any tenant member.
    """
    if summary is None:
        return None
    copy = dict(summary)
    copy["findings"] = [
        {**item, "money": None} for item in (summary.get("findings") or [])
    ]
    copy["money_redacted"] = True
    return copy


def _check_tenant(tenant_id):
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    return tenant_id


async def record_reconciliation(db, tenant_id, *, report, drill_kind=None):
    """Store one reconciliation report.

    Idempotent per (tenant, drill run, forecast record): a report
    already stored for the same pair is not stored twice —
    re-reconciling the same pair returns the existing row.
    Reports are immutable facts; a re-run never edits a stored row
    (acknowledge state included). Returns ``{"created", "reconciliation"}``.
    """
    _check_tenant(tenant_id)
    if not isinstance(report, dict) or not isinstance(report.get("findings"), list):
        raise ValueError("report must be a reconcile() result dict.")
    drill_run_id = report.get("drill_run_id")
    if not isinstance(drill_run_id, str) or not drill_run_id:
        raise ValueError("report needs a drill_run_id.")
    try:
        drill_run_uuid = UUID(drill_run_id)
    except ValueError as exc:
        raise ValueError(f"drill_run_id must be a UUID, got {drill_run_id!r}.") from exc
    forecast_record_id = report.get("forecast_record_id")
    if not isinstance(forecast_record_id, str) or not forecast_record_id:
        raise ValueError("report needs a forecast_record_id.")
    try:
        forecast_record_uuid = UUID(forecast_record_id)
    except ValueError as exc:
        raise ValueError(
            f"forecast_record_id must be a UUID, got {forecast_record_id!r}.") from exc
    existing = await db.scalar(select(ForecastReconciliation).where(
        ForecastReconciliation.tenant_id == tenant_id,
        ForecastReconciliation.drill_run_id == drill_run_uuid,
        ForecastReconciliation.forecast_record_id == forecast_record_uuid,
    ))
    if existing is not None:
        return {"created": False, "reconciliation": reconciliation_summary_row(existing)}
    row = ForecastReconciliation(
        tenant_id=tenant_id,
        forecast_record_id=forecast_record_uuid,
        drill_run_id=drill_run_uuid,
        drill_kind=drill_kind,
        forecast_fingerprint=report["forecast_fingerprint"],
        matches=bool(report.get("matches")),
        worst_severity=report.get("worst_severity"),
        findings=report["findings"],
        counts=report["counts"],
        adjusted_drift_score=report.get("adjusted_drift_score"),
    )
    db.add(row)
    await db.flush()
    return {"created": True, "reconciliation": reconciliation_summary_row(row)}


async def evaluate_reconciliation(db, tenant_id, *, forecast_record, g_comparison,
                                  drill_kind=None, now=None):
    """Reconcile one stored forecast with one drill run and store it.

    The worker/CLI calls this with a stored ``ForecastRecord`` row
    and the plain dict Step G's ``drill_comparison`` returns for
    that run. Runs the pure reconciliation, stores the report
    idempotently, folds the miss penalty into the forecast
    record's drift score (raised only — a matching drill leaves
    the score alone), and prunes old reports. Returns the pure
    report plus ``recorded`` (the store result),
    ``adjusted_drift_score`` (the record's score after the
    penalty, or its stored score when nothing changed), and
    ``summary`` (the one-line text). With a matching forecast and
    drill, ``recorded["reconciliation"]["findings"]`` is empty and
    ``matches`` is True — nothing enters the queue.
    """
    _check_tenant(tenant_id)
    if not isinstance(forecast_record, ForecastRecord):
        raise ValueError("forecast_record must be a ForecastRecord row.")
    if forecast_record.tenant_id != tenant_id:
        raise ValueError("forecast_record belongs to a different tenant.")
    report = reconciliation.reconcile(
        forecast_record.forecast, g_comparison,
        tenant_id=tenant_id,
        forecast_record_id=str(forecast_record.id),
    )
    if report["findings"]:
        adjusted = reconciliation.apply_miss_penalty(
            forecast_record.drift_score, report["findings"])
    else:
        adjusted = forecast_record.drift_score
    report["adjusted_drift_score"] = adjusted if report["findings"] else None
    recorded = await record_reconciliation(
        db, tenant_id, report=report, drill_kind=drill_kind)
    if report["findings"] and adjusted is not None and (
            forecast_record.drift_score is None or adjusted > forecast_record.drift_score):
        forecast_record.drift_score = adjusted
        await db.flush()
    await prune_reconciliations(db, tenant_id, now=now)
    await db.commit()
    return {**report, "recorded": recorded,
            "adjusted_drift_score": adjusted,
            "summary": reconciliation.reconciliation_summary(report)}


async def list_reconciliations(db, tenant_id, *, limit=30, acknowledged=None,
                               severity=None, drill_run_id=None):
    """Stored reconciliation reports, newest first, with optional filters."""
    _check_tenant(tenant_id)
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    query = select(ForecastReconciliation).where(
        ForecastReconciliation.tenant_id == tenant_id)
    if acknowledged is not None:
        query = query.where(
            ForecastReconciliation.acknowledged.is_(bool(acknowledged)))
    if severity is not None:
        if severity not in reconciliation.SEVERITIES:
            raise ValueError(f"unknown severity: {severity!r}.")
        query = query.where(ForecastReconciliation.worst_severity == severity)
    if drill_run_id is not None:
        if not isinstance(drill_run_id, UUID):
            raise ValueError(f"drill_run_id must be a UUID, got {drill_run_id!r}.")
        query = query.where(ForecastReconciliation.drill_run_id == drill_run_id)
    rows = (
        await db.scalars(
            query.order_by(ForecastReconciliation.created_at.desc(),
                           ForecastReconciliation.id.desc())
            .limit(clamped)
        )
    ).all()
    return [reconciliation_summary_row(row) for row in rows]


async def reconciliation_counts(db, tenant_id):
    """Review-queue counts for one tenant (for the queue badge)."""
    _check_tenant(tenant_id)
    rows = (
        await db.scalars(
            select(ForecastReconciliation).where(
                ForecastReconciliation.tenant_id == tenant_id)
        )
    ).all()
    counts = {"total": len(rows), "unacknowledged": 0, "acknowledged": 0,
              "matches": 0, "mismatches": 0, "unacknowledged_critical": 0}
    for severity in reconciliation.SEVERITIES:
        counts[severity] = 0
    for row in rows:
        if row.worst_severity is not None:
            counts[row.worst_severity] += 1
        if row.acknowledged:
            counts["acknowledged"] += 1
        else:
            counts["unacknowledged"] += 1
            if row.worst_severity == "critical":
                counts["unacknowledged_critical"] += 1
        if row.matches:
            counts["matches"] += 1
        else:
            counts["mismatches"] += 1
    return counts


async def get_reconciliation(db, tenant_id, reconciliation_id):
    """One stored reconciliation report, or ``None``."""
    _check_tenant(tenant_id)
    if not isinstance(reconciliation_id, UUID):
        raise ValueError(f"reconciliation_id must be a UUID, got {reconciliation_id!r}.")
    row = await db.scalar(select(ForecastReconciliation).where(
        ForecastReconciliation.tenant_id == tenant_id,
        ForecastReconciliation.id == reconciliation_id,
    ))
    return reconciliation_summary_row(row) if row is not None else None


async def acknowledge_reconciliation(db, tenant_id, reconciliation_id, *, by):
    """Mark one reconciliation report as seen by one tenant member.

    Only the queue row changes (``acknowledged`` /
    ``acknowledged_at`` / ``acknowledged_by``) — the report, the
    forecast, and the drill record are untouched. Returns the
    stored summary, or ``None`` when the report does not belong
    to this tenant. Acknowledging an already-acknowledged report
    refreshes the who/when (the latest reviewer is the one on
    record).
    """
    _check_tenant(tenant_id)
    if not isinstance(reconciliation_id, UUID):
        raise ValueError(f"reconciliation_id must be a UUID, got {reconciliation_id!r}.")
    if not isinstance(by, UUID):
        raise ValueError(f"by must be a UUID, got {by!r}.")
    row = await db.scalar(select(ForecastReconciliation).where(
        ForecastReconciliation.tenant_id == tenant_id,
        ForecastReconciliation.id == reconciliation_id,
    ))
    if row is None:
        return None
    row.acknowledged = True
    row.acknowledged_at = datetime.now(UTC)
    row.acknowledged_by = by
    await db.flush()
    await db.commit()
    return reconciliation_summary_row(row)


async def prune_reconciliations(db, tenant_id, *, keep=DEFAULT_RECONCILIATION_RETENTION_COUNT,
                                max_age_days=DEFAULT_RECONCILIATION_RETENTION_DAYS, now=None):
    """Delete reconciliation reports beyond the retention rule; returns how many.

    A report leaves when it is older than ``max_age_days`` OR when
    it falls outside the newest ``keep`` reports for the tenant.
    Derived data only — a pruned report is re-derived by
    re-running the reconciliation over the stored forecast record
    and the drill comparison.
    """
    _check_tenant(tenant_id)
    if keep < 1:
        raise ValueError("keep must be at least 1.")
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative.")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=max_age_days)
    rows = (
        await db.scalars(
            select(ForecastReconciliation)
            .where(ForecastReconciliation.tenant_id == tenant_id)
            .order_by(ForecastReconciliation.created_at.desc(),
                      ForecastReconciliation.id.desc())
        )
    ).all()
    doomed = [
        row.id
        for index, row in enumerate(rows)
        if index >= keep or row.created_at < cutoff
    ]
    if doomed:
        await db.execute(
            delete(ForecastReconciliation).where(ForecastReconciliation.id.in_(doomed))
        )
    return len(doomed)
