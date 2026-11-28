"""Drill-reconciliation review queue service (Step K v2 slice 5).

Async service over the stored reconciliation findings. Records the
findings of one reconciliation
(:func:`app.digital_twin.reconciliation.reconcile`), lists the
review queue, counts it, acknowledges findings, and prunes old
ones. The scheduled entry point is
:func:`evaluate_reconciliation`: the twin service / worker calls it
on a schedule (or after a drill run) with the twin simulation and
the Step G drill comparison for that run.

No endpoint and no service function here approves, dispatches,
grants access, or pays anything, and nothing here ever rewrites a
drill record or a twin version. Acknowledging a finding only marks
the queue row as seen (who + when) — the finding itself stays in
the queue until retention prunes it. Money differences are
redacted for non-owners at read time (see
:func:`redact_money_for_viewer`); the finding ``detail`` text
never contains an amount in the first place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select

from app.db.twin_reconciliation_models import TwinReconciliationFinding
from app.digital_twin import reconciliation

#: Finding retention defaults (same 90-day age rule as health
#: alerts; 100 findings is the per-tenant cap — findings are small
#: rows and a queue shorter than the version history would hide
#: repeat disagreements).
DEFAULT_FINDING_RETENTION_COUNT = 100
DEFAULT_FINDING_RETENTION_DAYS = 90

#: Hard cap on list responses so the review-queue list stays bounded.
MAX_LIST_LIMIT = 100

__all__ = [
    "DEFAULT_FINDING_RETENTION_COUNT",
    "DEFAULT_FINDING_RETENTION_DAYS",
    "MAX_LIST_LIMIT",
    "acknowledge_finding",
    "evaluate_reconciliation",
    "finding_summary",
    "list_findings",
    "prune_findings",
    "record_reconciliation",
    "reconciliation_counts",
    "redact_money_for_viewer",
]


def finding_summary(row):
    """Small read-only summary of a stored finding row.

    ``money_difference`` is included for the owner; non-owners get
    the redacted copy (see :func:`redact_money_for_viewer`). The
    ``detail`` text itself never contains an amount.
    """
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "drill_run_id": str(row.drill_run_id),
        "scenario_kind": row.scenario_kind,
        "simulation_fingerprint": row.simulation_fingerprint,
        "code": row.code,
        "resource_id": row.resource_id,
        "severity": row.severity,
        "source": row.source,
        "detail": row.detail,
        "money_difference": row.money_difference,
        "money_redacted": False,
        "fingerprint": row.fingerprint,
        "acknowledged": row.acknowledged,
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_by": str(row.acknowledged_by) if row.acknowledged_by else None,
        "created_at": row.created_at.isoformat(),
    }


def redact_money_for_viewer(summary):
    """Copy of a finding summary with the money difference hidden.

    ``None`` in → ``None`` out. The owner-only rule (plan §4):
    exposure amounts stay owner-only; the finding code, severity,
    and detail are safe for any tenant member.
    """
    if summary is None:
        return None
    copy = dict(summary)
    copy["money_difference"] = None
    copy["money_redacted"] = True
    return copy


def _check_tenant(tenant_id):
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    return tenant_id


async def record_reconciliation(db, tenant_id, *, report):
    """Store the findings of one reconciliation report.

    Idempotent per (tenant, fingerprint): a finding already stored
    for the same drill run + simulation + code + resource is not
    stored twice — re-reconciling the same run returns the existing
    rows. Findings are immutable facts; a re-run never edits a
    stored row (acknowledge state included). Returns ``{"created",
    "existing", "findings"}`` with stored summaries, newest first.
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
    findings = report["findings"]
    if not findings:
        return {"created": [], "existing": [], "findings": []}
    stored = (
        await db.scalars(
            select(TwinReconciliationFinding).where(
                TwinReconciliationFinding.tenant_id == tenant_id,
                TwinReconciliationFinding.fingerprint.in_(
                    [item["fingerprint"] for item in findings]),
            )
        )
    ).all()
    existing = {row.fingerprint: row for row in stored}
    created = []
    for item in findings:
        if item["fingerprint"] in existing:
            continue
        row = TwinReconciliationFinding(
            tenant_id=tenant_id,
            drill_run_id=drill_run_uuid,
            scenario_kind=report.get("scenario_kind"),
            simulation_fingerprint=report["simulation_fingerprint"],
            code=item["code"],
            resource_id=item["resource_id"],
            severity=item["severity"],
            source=item["source"],
            detail=item["detail"],
            money_difference=item.get("money"),
            fingerprint=item["fingerprint"],
        )
        db.add(row)
        created.append(row)
    await db.flush()
    rows = (
        await db.scalars(
            select(TwinReconciliationFinding)
            .where(
                TwinReconciliationFinding.tenant_id == tenant_id,
                TwinReconciliationFinding.fingerprint.in_(
                    [item["fingerprint"] for item in findings]),
            )
            .order_by(TwinReconciliationFinding.created_at.desc(),
                      TwinReconciliationFinding.id.desc())
        )
    ).all()
    summaries = [finding_summary(row) for row in rows]
    created_ids = {str(row.id) for row in created}
    return {
        "created": [s for s in summaries if s["id"] in created_ids],
        "existing": [s for s in summaries if s["id"] not in created_ids],
        "findings": summaries,
    }


async def list_findings(db, tenant_id, *, limit=30, acknowledged=None, severity=None):
    """Stored findings, newest first, with optional filters."""
    _check_tenant(tenant_id)
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    query = select(TwinReconciliationFinding).where(
        TwinReconciliationFinding.tenant_id == tenant_id)
    if acknowledged is not None:
        query = query.where(TwinReconciliationFinding.acknowledged.is_(bool(acknowledged)))
    if severity is not None:
        if severity not in reconciliation.SEVERITIES:
            raise ValueError(f"unknown severity: {severity!r}.")
        query = query.where(TwinReconciliationFinding.severity == severity)
    rows = (
        await db.scalars(
            query.order_by(TwinReconciliationFinding.created_at.desc(),
                           TwinReconciliationFinding.id.desc())
            .limit(clamped)
        )
    ).all()
    return [finding_summary(row) for row in rows]


async def reconciliation_counts(db, tenant_id):
    """Review-queue counts for one tenant (for the queue badge)."""
    _check_tenant(tenant_id)
    rows = (
        await db.scalars(
            select(TwinReconciliationFinding).where(
                TwinReconciliationFinding.tenant_id == tenant_id)
        )
    ).all()
    counts = {"total": len(rows), "unacknowledged": 0, "acknowledged": 0}
    for severity in reconciliation.SEVERITIES:
        counts[severity] = 0
    counts["unacknowledged_critical"] = 0
    for row in rows:
        counts[row.severity] += 1
        if row.acknowledged:
            counts["acknowledged"] += 1
        else:
            counts["unacknowledged"] += 1
            if row.severity == "critical":
                counts["unacknowledged_critical"] += 1
    return counts


async def acknowledge_finding(db, tenant_id, finding_id, *, by):
    """Mark one finding as seen by one tenant member.

    Only the queue row changes (``acknowledged`` /
    ``acknowledged_at`` / ``acknowledged_by``) — the finding, the
    twin, and the drill record are untouched. Returns the stored
    summary, or ``None`` when the finding does not belong to this
    tenant. Acknowledging an already-acknowledged finding refreshes
    the who/when (the latest reviewer is the one on record).
    """
    _check_tenant(tenant_id)
    if not isinstance(finding_id, UUID):
        raise ValueError(f"finding_id must be a UUID, got {finding_id!r}.")
    if not isinstance(by, UUID):
        raise ValueError(f"by must be a UUID, got {by!r}.")
    row = await db.scalar(
        select(TwinReconciliationFinding).where(
            TwinReconciliationFinding.tenant_id == tenant_id,
            TwinReconciliationFinding.id == finding_id,
        )
    )
    if row is None:
        return None
    row.acknowledged = True
    row.acknowledged_at = datetime.now(UTC)
    row.acknowledged_by = by
    await db.flush()
    return finding_summary(row)


async def prune_findings(db, tenant_id, *, keep=DEFAULT_FINDING_RETENTION_COUNT,
                         max_age_days=DEFAULT_FINDING_RETENTION_DAYS, now=None):
    """Delete findings beyond the retention rule; returns how many.

    A finding leaves when it is older than ``max_age_days`` OR when
    it falls outside the newest ``keep`` findings for the tenant.
    Derived data only — a pruned finding is re-derived by
    re-running the reconciliation over the stored simulation and
    drill records.
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
            select(TwinReconciliationFinding)
            .where(TwinReconciliationFinding.tenant_id == tenant_id)
            .order_by(TwinReconciliationFinding.created_at.desc(),
                      TwinReconciliationFinding.id.desc())
        )
    ).all()
    doomed = [
        row.id
        for index, row in enumerate(rows)
        if index >= keep or row.created_at < cutoff
    ]
    if doomed:
        await db.execute(
            delete(TwinReconciliationFinding).where(TwinReconciliationFinding.id.in_(doomed))
        )
    return len(doomed)


async def evaluate_reconciliation(db, tenant_id, *, simulation, drill_observation,
                                  g_comparison=None, drill_run_id, scenario_kind=None,
                                  now=None):
    """Reconcile one drill run and store its findings (scheduled entry).

    The twin service / worker calls this on a schedule (or after a
    drill run) with the twin simulation and the Step G drill
    comparison for that run. Runs the pure reconciliation, stores
    new findings idempotently, and prunes old ones. Returns the
    pure report plus ``recorded`` (the store result) and
    ``summary`` (the one-line text). With a matching simulation
    and drill, ``recorded["findings"]`` is empty and ``matches``
    is True — nothing enters the queue.
    """
    _check_tenant(tenant_id)
    report = reconciliation.reconcile(
        simulation, drill_observation,
        tenant_id=tenant_id, drill_run_id=drill_run_id,
        g_comparison=g_comparison, scenario_kind=scenario_kind,
    )
    recorded = await record_reconciliation(db, tenant_id, report=report)
    await prune_findings(db, tenant_id, now=now)
    return {**report, "recorded": recorded,
            "summary": reconciliation.reconciliation_summary(report)}
