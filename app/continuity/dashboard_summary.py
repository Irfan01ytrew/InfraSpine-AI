"""Business continuity dashboard aggregator (Step AJ, slice 1).

Pure functions, standard library only: no I/O, no database, no network,
no secrets. The dashboard is read-only — this module only rolls up plain
inputs (risks, incidents, approvals, recoveries, health) into one summary
dict a later slice can serve through an API and a frontend page.

Vocabulary matches the existing stores:
- severities: ``critical`` / ``high`` / ``medium`` / ``low``.
- incident statuses: recovery-incident statuses; ``resolved`` and
  ``cancelled`` are terminal (``app/recovery/policy.py::TERMINAL``).
- recovery statuses: continuity-run statuses
  (``pending`` / ``active`` / ``suspended`` / ``rejected`` / ``closed``).
- risk sources: ``gap`` (continuity gap), ``incident`` (recovery incident),
  ``alert`` (Step AF alert), ``prediction`` (Step AH risk prediction).

Validation is strict on purpose: an unknown severity/status, a negative
step count, or ``verified_steps > total_steps`` raises ``ValueError`` —
a dashboard must never silently show a wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SEVERITIES = ("critical", "high", "medium", "low")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
RISK_SOURCES = ("gap", "incident", "alert", "prediction")
TERMINAL_INCIDENT_STATUSES = ("resolved", "cancelled")
RECOVERY_STATUSES = ("pending", "active", "suspended", "rejected", "closed")
OPEN_RECOVERY_STATUSES = ("pending", "active", "suspended")
EXPIRING_SOON = timedelta(hours=24)
HIGHLIGHT_LIMIT = 6
TOP_LIMIT = 5


@dataclass(frozen=True)
class RiskInput:
    """One open risk, from any source (gap, incident, alert, prediction)."""

    id: str
    title: str
    severity: str
    category: str = ""
    source: str = "gap"
    affected: tuple = ()
    has_plan: bool = False


@dataclass(frozen=True)
class IncidentInput:
    """One recovery incident."""

    id: str
    title: str
    severity: str
    status: str = "detected"
    overdue: bool = False


@dataclass(frozen=True)
class ApprovalInput:
    """One pending approval the viewer may decide."""

    id: str
    action_type: str
    subject: str = ""
    expires_at: datetime | None = None
    own_request: bool = False


@dataclass(frozen=True)
class RecoveryInput:
    """One continuity run / recovery in flight (or closed)."""

    id: str
    title: str
    status: str = "pending"
    verified_steps: int = 0
    total_steps: int = 0
    rto_minutes: int | None = None
    elapsed_minutes: float | None = None


def _require_severity(severity: str) -> str:
    if severity not in _SEVERITY_RANK:
        raise ValueError(f"unknown severity: {severity!r}")
    return severity


def _require_non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_risk(risk: RiskInput) -> None:
    _require_non_empty(risk.id, "risk id")
    _require_non_empty(risk.title, "risk title")
    _require_severity(risk.severity)
    if risk.source not in RISK_SOURCES:
        raise ValueError(f"unknown risk source: {risk.source!r}")


def _validate_incident(incident: IncidentInput) -> None:
    _require_non_empty(incident.id, "incident id")
    _require_non_empty(incident.title, "incident title")
    _require_severity(incident.severity)


def _validate_approval(approval: ApprovalInput) -> None:
    _require_non_empty(approval.id, "approval id")
    _require_non_empty(approval.action_type, "approval action_type")
    if approval.expires_at is not None and not isinstance(
        approval.expires_at, datetime
    ):
        raise ValueError("approval expires_at must be a datetime or None")


def _validate_recovery(recovery: RecoveryInput) -> None:
    _require_non_empty(recovery.id, "recovery id")
    _require_non_empty(recovery.title, "recovery title")
    if recovery.status not in RECOVERY_STATUSES:
        raise ValueError(f"unknown recovery status: {recovery.status!r}")
    if recovery.verified_steps < 0 or recovery.total_steps < 0:
        raise ValueError("recovery step counts must not be negative")
    if recovery.verified_steps > recovery.total_steps:
        raise ValueError("verified_steps must not exceed total_steps")
    if recovery.rto_minutes is not None and recovery.rto_minutes < 0:
        raise ValueError("rto_minutes must not be negative")
    if recovery.elapsed_minutes is not None and recovery.elapsed_minutes < 0:
        raise ValueError("elapsed_minutes must not be negative")


def recovery_progress(recovery: RecoveryInput) -> float | None:
    """Progress of one recovery in percent (1 decimal), or None with no steps."""
    _validate_recovery(recovery)
    if recovery.total_steps == 0:
        return None
    return round(100.0 * recovery.verified_steps / recovery.total_steps, 1)


def recovery_at_risk(recovery: RecoveryInput) -> bool:
    """True when an open recovery has run past its RTO (both must be known)."""
    _validate_recovery(recovery)
    if recovery.status not in ("active", "suspended"):
        return False
    if recovery.rto_minutes is None or recovery.elapsed_minutes is None:
        return False
    return recovery.elapsed_minutes > recovery.rto_minutes


def _risk_out(risk: RiskInput) -> dict:
    return {
        "id": risk.id,
        "title": risk.title,
        "severity": risk.severity,
        "category": risk.category,
        "source": risk.source,
        "affected": list(risk.affected),
        "has_plan": risk.has_plan,
    }


def _incident_out(incident: IncidentInput) -> dict:
    return {
        "id": incident.id,
        "title": incident.title,
        "severity": incident.severity,
        "status": incident.status,
        "overdue": incident.overdue,
    }


def _approval_out(approval: ApprovalInput) -> dict:
    return {
        "id": approval.id,
        "action_type": approval.action_type,
        "subject": approval.subject,
        "expires_at": (
            None if approval.expires_at is None else approval.expires_at.isoformat()
        ),
        "own_request": approval.own_request,
    }


def _recovery_out(recovery: RecoveryInput) -> dict:
    return {
        "id": recovery.id,
        "title": recovery.title,
        "status": recovery.status,
        "verified_steps": recovery.verified_steps,
        "total_steps": recovery.total_steps,
        "progress_percent": recovery_progress(recovery),
        "at_risk": recovery_at_risk(recovery),
    }


def build_dashboard_summary(
    *,
    risks=(),
    incidents=(),
    approvals=(),
    recoveries=(),
    health=None,
    now: datetime | None = None,
) -> dict:
    """Roll risks, incidents, approvals, recoveries and health into one summary.

    ``health`` is the Step G health roll-up dict passed through unchanged
    (or ``None`` when unavailable). ``now`` defaults to the current UTC
    time; pass it in tests for deterministic expiry checks.
    """
    now = now or datetime.now(UTC)
    risks = list(risks)
    incidents = list(incidents)
    approvals = list(approvals)
    recoveries = list(recoveries)
    for risk in risks:
        _validate_risk(risk)
    for incident in incidents:
        _validate_incident(incident)
    for approval in approvals:
        _validate_approval(approval)
    for recovery in recoveries:
        _validate_recovery(recovery)

    # --- risks ---
    risks_by_severity = {
        name: sum(1 for r in risks if r.severity == name) for name in SEVERITIES
    }
    risks_by_severity = {k: v for k, v in risks_by_severity.items() if v}
    uncovered_critical = sum(
        1 for r in risks if r.severity == "critical" and not r.has_plan
    )
    top_risks = sorted(
        risks, key=lambda r: (_SEVERITY_RANK[r.severity], r.title.lower())
    )[:TOP_LIMIT]

    # --- incidents ---
    active_incidents = [
        i for i in incidents if i.status not in TERMINAL_INCIDENT_STATUSES
    ]
    incidents_by_severity = {
        name: sum(1 for i in active_incidents if i.severity == name)
        for name in SEVERITIES
    }
    incidents_by_severity = {k: v for k, v in incidents_by_severity.items() if v}
    overdue_incidents = sum(1 for i in active_incidents if i.overdue)
    critical_active = sum(
        1 for i in active_incidents if i.severity == "critical"
    )
    top_incidents = sorted(
        active_incidents,
        key=lambda i: (not i.overdue, _SEVERITY_RANK[i.severity], i.title.lower()),
    )[:TOP_LIMIT]

    # --- approvals ---
    pending_approvals = list(approvals)
    expiring_soon = sum(
        1
        for a in pending_approvals
        if a.expires_at is not None and now < a.expires_at <= now + EXPIRING_SOON
    )
    own_requests = sum(1 for a in pending_approvals if a.own_request)

    # --- recovery ---
    active_recoveries = [
        r for r in recoveries if r.status in OPEN_RECOVERY_STATUSES
    ]
    closed_recoveries = [r for r in recoveries if r.status == "closed"]
    progresses = [
        p for p in (recovery_progress(r) for r in recoveries) if p is not None
    ]
    average_progress = (
        round(sum(progresses) / len(progresses), 1) if progresses else None
    )
    at_risk_recoveries = [r for r in recoveries if recovery_at_risk(r)]

    # --- overall status ---
    if (
        uncovered_critical
        or any(r.severity == "critical" for r in risks)
        or critical_active
        or at_risk_recoveries
    ):
        overall = "critical"
    elif risks or active_incidents or pending_approvals or active_recoveries:
        overall = "attention"
    else:
        overall = "ok"

    # --- highlights: worst first, plain language ---
    highlights = []
    if uncovered_critical:
        highlights.append(
            f"{uncovered_critical} critical risk(s) have no recovery plan"
        )
    if overdue_incidents:
        highlights.append(
            f"{overdue_incidents} active incident(s) overdue their SLA"
        )
    if critical_active:
        highlights.append(f"{critical_active} active critical incident(s)")
    if at_risk_recoveries:
        names = ", ".join(r.title for r in at_risk_recoveries[:2])
        highlights.append(
            f"{len(at_risk_recoveries)} recovery(ies) past RTO: {names}"
        )
    if expiring_soon:
        highlights.append(
            f"{expiring_soon} approval(s) expiring within 24 hours"
        )
    if pending_approvals:
        highlights.append(f"{len(pending_approvals)} approval(s) pending")
    if active_recoveries:
        highlights.append(f"{len(active_recoveries)} recovery(ies) in progress")
    highlights = highlights[:HIGHLIGHT_LIMIT]

    return {
        "overall_status": overall,
        "generated_at": now.isoformat(),
        "risks": {
            "total": len(risks),
            "by_severity": risks_by_severity,
            "uncovered_critical": uncovered_critical,
            "top": [_risk_out(r) for r in top_risks],
        },
        "incidents": {
            "total_active": len(active_incidents),
            "by_severity": incidents_by_severity,
            "overdue": overdue_incidents,
            "top": [_incident_out(i) for i in top_incidents],
        },
        "approvals": {
            "pending": len(pending_approvals),
            "expiring_soon": expiring_soon,
            "own_requests": own_requests,
        },
        "recovery": {
            "active": len(active_recoveries),
            "closed": len(closed_recoveries),
            "average_progress_percent": average_progress,
            "at_risk": len(at_risk_recoveries),
            "items": [_recovery_out(r) for r in recoveries],
        },
        "health": health,
        "highlights": highlights,
    }


def summarize_dashboard(summary: dict) -> str:
    """One-line plain-language header for a dashboard summary."""
    risks = summary["risks"]
    incidents = summary["incidents"]
    approvals = summary["approvals"]
    recovery = summary["recovery"]
    progress = recovery["average_progress_percent"]
    progress_text = (
        f"{progress}% complete" if progress is not None else "no recovery steps"
    )
    return (
        f"{summary['overall_status']}: "
        f"{risks['total']} open risk(s) "
        f"({risks['uncovered_critical']} critical uncovered), "
        f"{incidents['total_active']} active incident(s) "
        f"({incidents['overdue']} overdue), "
        f"{approvals['pending']} approval(s) pending, "
        f"recovery {progress_text}"
    )
