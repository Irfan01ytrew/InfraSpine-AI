"""Drill reconciliation (Step L slice 7).

Pure functions; stdlib only. No imports from ``app.db``, no I/O, no
network, no secrets, and no import from ``app.continuity`` — Step G
is not merged on this branch yet, so the Step G side arrives as the
plain dict that Step G's ``drill_comparison`` returns (see
``app/continuity/drills.py`` on the ``agent/stepg-continuity``
branch). When G merges, the caller passes that dict straight in;
nothing here needs to change.

The problem this solves: an L forecast says "if this person, system
or supplier is unavailable, these processes are affected" — a drill
is what actually happened. The reconciliation compares one stored
forecast against one real Step G drill run and reports the
differences as review-queue findings:

* ``forecasted_not_drilled`` — the forecast named an entity the
  drill did not recover (the forecast over-predicted). Critical when
  the entity is a process due within the forecast horizon, warning
  otherwise.
* ``drilled_not_forecasted`` — the drill recovered an entity the
  forecast never listed (the forecast missed it). Always warning:
  this is the miss that feeds back into forecast trust.
* Step G's own timing differences (``rto_missed_in_drill`` and
  friends) are adapted verbatim and graded by severity, the same
  grading Step K v2 slice 5 uses — the codes are G's contract.

Differences go to the review queue
(:mod:`app.predictive_continuity.reconciliation_records`) — they are
never auto-applied, and the loop never rewrites a drill record.
Acknowledging a reconciliation only marks the queue row as seen.

Money: a ``forecasted_not_drilled`` finding for a process carries
that process's money at risk (exposure totals only, never an
account number) in its ``money`` field, and its ``detail`` text
carries NO amount — the API redacts ``money`` for non-owners, the
same owner-only rule as forecasts and proposals. Detail text is
plain ASCII only: the throwaway test cluster is SQL_ASCII, so a
non-ASCII detail would fail the insert (slice 6 lesson).

Trust feedback: a drill miss means the forecast was wrong, so
:func:`miss_penalty` turns findings into a drift-score penalty and
:func:`apply_miss_penalty` folds it into the forecast record's
stored drift score (raised, never lowered — a matching drill never
lowers drift, because drift measures twin change, not drill
agreement). The penalty is computed here; the service applies it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

#: Finding severities, worst first.
SEVERITIES = ("info", "warning", "critical")

#: Which side of the comparison produced the finding.
SOURCES = ("forecast", "continuity")

#: Hard cap on findings in one reconciliation report, so a wild
#: drill cannot flood the review queue. Reports that hit the cap
#: say so (``truncated: True``) instead of silently dropping rows.
MAX_FINDINGS = 500

#: L-side finding codes.
CODE_FORECASTED_NOT_DRILLED = "forecasted_not_drilled"
CODE_DRILLED_NOT_FORECASTED = "drilled_not_forecasted"

#: Step G comparison codes (``app/continuity/drills.py``,
#: ``compare_drill_to_simulation``) graded by severity. A drill that
#: beats its forecast is reported too (``drill_met_rto_simulation_missed``
#: is ``info``) — a forecast that keeps being wrong must get noticed
#: instead of quietly staying wrong. An unknown code is a
#: ``ValueError``: a difference we cannot grade must not silently
#: enter the queue with a guessed severity.
G_CODE_SEVERITY = {
    "affected_not_recovered": "critical",
    "rto_missed_in_drill": "critical",
    "drill_missed_rto_simulation_met": "critical",
    "mtpd_exceeded_in_drill": "critical",
    "data_loss_exceeds_rpo": "critical",
    "recovered_not_in_simulation": "warning",
    "drill_met_rto_simulation_missed": "info",
}

#: Miss-penalty weights (drift-score points). A real miss — the drill
#: hit something the forecast never named — weighs most; a critical
#: over-prediction weighs next; any other warning weighs least.
#: Info findings never move trust.
MISS_POINTS = 15
CRITICAL_POINTS = 10
WARNING_POINTS = 5

_FINGERPRINT_HEX = set("0123456789abcdef")


def _as_str(value, field):
    """A UUID or non-empty string, normalized to ``str``."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{field} must be a non-empty string or UUID, got {value!r}.")


def _check_fingerprint(value, field):
    """A 64-hex fingerprint (the forecast/snapshot fingerprint)."""
    text = _as_str(value, field)
    if len(text) != 64 or any(ch not in _FINGERPRINT_HEX for ch in text):
        raise ValueError(f"{field} must be a 64-hex fingerprint, got {text!r}.")
    return text


def finding_fingerprint(tenant_id, drill_run_id, forecast_fingerprint, code, resource_id,
                        money=None):
    """Stable dedupe fingerprint for one finding.

    SHA-256 over the canonical JSON of
    ``(tenant, drill run, forecast fingerprint, code, resource,
    money)``, so the same finding re-reconciled for the same drill
    run and the same forecast is stored once. Two different
    forecasts of the same drill run produce different fingerprints
    and both stay in the queue — and two money findings for
    different currencies are different findings, not one row
    (``money`` is part of the identity for that reason).
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        _as_str(drill_run_id, "drill_run_id"),
        _check_fingerprint(forecast_fingerprint, "forecast_fingerprint"),
        code,
        resource_id or "",
        money or {},
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ReconciliationFinding:
    """One unified review-queue finding (pure value, not a DB row)."""

    code: str
    resource_id: str | None
    severity: str
    source: str
    detail: str
    fingerprint: str
    money: dict | None = None

    def as_dict(self):
        return {
            "code": self.code,
            "resource_id": self.resource_id,
            "severity": self.severity,
            "source": self.source,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
            "money": self.money,
        }


def _finding(tenant_id, drill_run_id, forecast_fingerprint, *, code, resource_id,
             severity, source, detail, money=None):
    return ReconciliationFinding(
        code=code,
        resource_id=resource_id,
        severity=severity,
        source=source,
        detail=detail,
        fingerprint=finding_fingerprint(
            tenant_id, drill_run_id, forecast_fingerprint, code, resource_id, money),
        money=money,
    )


def _validate_forecast(forecast):
    """Check a stored forecast dict; return (processes, affected, fingerprint).

    ``processes`` maps process id -> process dict (for money at risk
    and due status); ``affected`` maps entity id -> (kind,
    due_status). A malformed forecast raises ``ValueError`` instead
    of producing a misleading reconciliation.
    """
    if not isinstance(forecast, dict):
        raise ValueError("forecast must be a dict.")
    fingerprint = _check_fingerprint(
        forecast.get("fingerprint"), "forecast fingerprint")
    processes_raw = forecast.get("processes")
    if not isinstance(processes_raw, list):
        raise ValueError("forecast needs a 'processes' list.")
    affected_raw = forecast.get("affected")
    if not isinstance(affected_raw, list):
        raise ValueError("forecast needs an 'affected' list.")
    processes = {}
    for entry in processes_raw:
        if not isinstance(entry, dict):
            raise ValueError("forecast processes must be dicts.")
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            raise ValueError("forecast process needs a non-empty string 'id'.")
        due_status = entry.get("due_status", "no_deadline")
        if not isinstance(due_status, str):
            raise ValueError(f"forecast process {pid!r} due_status must be a string.")
        money = entry.get("money_at_risk") or {}
        if not isinstance(money, dict):
            raise ValueError(f"forecast process {pid!r} money_at_risk must be a dict.")
        totals = money.get("totals") or {}
        if not isinstance(totals, dict):
            raise ValueError(f"forecast process {pid!r} money totals must be a dict.")
        processes[pid] = {
            "due_status": due_status,
            "money": {k: v for k, v in totals.items() if isinstance(v, (int, float)) and v},
        }
    affected = {}
    for entry in affected_raw:
        if not isinstance(entry, dict):
            raise ValueError("forecast affected entries must be dicts.")
        entity = entry.get("entity")
        if not isinstance(entity, dict):
            raise ValueError("forecast affected entry needs an 'entity' dict.")
        eid = entity.get("id")
        if not isinstance(eid, str) or not eid:
            raise ValueError("forecast affected entity needs a non-empty string 'id'.")
        kind = entity.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"forecast affected entity {eid!r} needs a 'kind'.")
        due_status = entry.get("due_status", "no_deadline")
        if not isinstance(due_status, str):
            raise ValueError(f"forecast affected entity {eid!r} due_status must be a string.")
        affected[eid] = (kind, due_status)
    return processes, affected, fingerprint


def _validate_drill(drill):
    """Check a Step G ``drill`` dict; return (affected_ids, recovered_ids)."""
    if not isinstance(drill, dict):
        raise ValueError("Step G comparison needs a 'drill' dict.")
    affected_ids = drill.get("affected_ids")
    recovered_ids = drill.get("recovered_ids")
    for name, value in (("affected_ids", affected_ids), ("recovered_ids", recovered_ids)):
        if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value):
            raise ValueError(f"Step G drill {name!r} must be a list of non-empty strings.")
    return set(affected_ids), set(recovered_ids)


def findings_from_forecast_and_drill(forecast, drill, *, tenant_id, drill_run_id,
                                     forecast_fingerprint=None):
    """Compare a stored forecast with the drill's observed outcome.

    ``forecast`` is the stored forecast dict (``ForecastRecord.forecast``);
    ``drill`` is the ``drill`` sub-dict of Step G's ``drill_comparison``
    result (``affected_ids`` = what G's simulation said would be hit,
    ``recovered_ids`` = what the drill actually recovered). One finding
    per forecast entity the drill did not recover, and one per drilled
    entity the forecast never named. Matched entities produce no
    finding — a forecast that agrees with the drill stays quiet.
    """
    processes, affected, fingerprint = _validate_forecast(forecast)
    if forecast_fingerprint is not None:
        fingerprint = _check_fingerprint(forecast_fingerprint, "forecast_fingerprint")
    _affected_ids, recovered = _validate_drill(drill)
    tenant = _as_str(tenant_id, "tenant_id")
    run = _as_str(drill_run_id, "drill_run_id")
    findings = []
    for entity_id in sorted(affected):
        if entity_id in recovered:
            continue
        kind, due_status = affected[entity_id]
        critical = kind == "process" and due_status == "due_within_horizon"
        money = None
        if kind == "process":
            money = processes.get(entity_id, {}).get("money") or None
        findings.append(_finding(
            tenant, run, fingerprint,
            code=CODE_FORECASTED_NOT_DRILLED, resource_id=entity_id,
            severity="critical" if critical else "warning", source="forecast",
            detail=f"forecast named {entity_id} but the drill did not recover it",
            money=money))
    for entity_id in sorted(recovered - set(affected)):
        findings.append(_finding(
            tenant, run, fingerprint,
            code=CODE_DRILLED_NOT_FORECASTED, resource_id=entity_id,
            severity="warning", source="forecast",
            detail=f"drill recovered {entity_id} but the forecast did not list it"))
    findings.sort(key=lambda item: (item.code, item.resource_id or ""))
    return findings


def findings_from_g_comparison(g_comparison, *, tenant_id, drill_run_id,
                               forecast_fingerprint):
    """Adapt a Step G ``drill_comparison`` result into findings.

    The G side's ``{code, resource_id, detail}`` differences are
    kept verbatim (code + detail) and graded by
    :data:`G_CODE_SEVERITY`; the resource id and the run id come
    from the same dict. A G comparison whose ``run_id`` names a
    different drill run than ``drill_run_id`` is a ``ValueError`` —
    the queue must never mix findings from two runs.
    """
    if not isinstance(g_comparison, dict):
        raise ValueError("Step G comparison must be a dict.")
    differences = g_comparison.get("differences")
    if not isinstance(differences, list):
        raise ValueError("Step G comparison needs a 'differences' list.")
    tenant = _as_str(tenant_id, "tenant_id")
    run = _as_str(drill_run_id, "drill_run_id")
    fingerprint = _check_fingerprint(forecast_fingerprint, "forecast_fingerprint")
    g_run_id = g_comparison.get("run_id")
    if g_run_id is not None and str(g_run_id) != run:
        raise ValueError(
            f"Step G comparison is for drill run {g_run_id!r}, not {run!r}.")
    findings = []
    for item in differences:
        if not isinstance(item, dict):
            raise ValueError("Step G comparison differences must be dicts.")
        code = item.get("code")
        if code not in G_CODE_SEVERITY:
            raise ValueError(f"unknown Step G difference code: {code!r}.")
        resource_id = item.get("resource_id")
        if resource_id is not None and not isinstance(resource_id, str):
            raise ValueError("Step G difference resource_id must be a string or null.")
        detail = item.get("detail")
        if not isinstance(detail, str) or not detail:
            raise ValueError("Step G difference detail must be a non-empty string.")
        findings.append(_finding(
            tenant, run, fingerprint,
            code=code, resource_id=resource_id,
            severity=G_CODE_SEVERITY[code], source="continuity",
            detail=detail))
    findings.sort(key=lambda item: (item.code, item.resource_id or ""))
    return findings


def reconcile(forecast, g_comparison, *, tenant_id, drill_run_id=None,
              forecast_record_id=None):
    """Reconcile one stored forecast against one real Step G drill.

    Runs the forecast-vs-drill comparison and, when the Step G
    comparison carries its own timing differences, adapts those
    too, then merges both sides into one deterministic finding
    list, deduped by fingerprint. ``matches`` is True only when
    neither side reports any difference.

    ``forecast`` is the stored forecast dict
    (``ForecastRecord.forecast``); ``g_comparison`` is the plain
    dict Step G's ``drill_comparison`` returns. The drill run id
    comes from the explicit argument or from the comparison's
    ``run_id`` — a reconciliation without one is a ``ValueError``,
    because findings must be tied to the drill run they came from.
    Nothing here writes anything: the caller stores the report
    through
    :func:`app.predictive_continuity.reconciliation_records.record_reconciliation`.
    """
    tenant = _as_str(tenant_id, "tenant_id")
    run = drill_run_id
    if run is None and isinstance(g_comparison, dict):
        run = g_comparison.get("run_id")
    run = _as_str(run, "drill_run_id")
    if forecast_record_id is not None:
        forecast_record_id = _as_str(forecast_record_id, "forecast_record_id")
    _processes, _affected, fingerprint = _validate_forecast(forecast)
    if not isinstance(g_comparison, dict):
        raise ValueError("Step G comparison must be a dict.")
    drill = g_comparison.get("drill")
    if drill is None:
        raise ValueError("Step G comparison needs a 'drill' dict.")
    findings = findings_from_forecast_and_drill(
        forecast, drill, tenant_id=tenant, drill_run_id=run,
        forecast_fingerprint=fingerprint)
    forecast_matches = not findings
    g_findings = findings_from_g_comparison(
        g_comparison, tenant_id=tenant, drill_run_id=run,
        forecast_fingerprint=fingerprint)
    g_matches = g_comparison.get("matches")
    if g_matches is not None and not isinstance(g_matches, bool):
        raise ValueError("Step G comparison 'matches' must be a boolean.")
    findings.extend(g_findings)
    seen = set()
    merged = []
    for item in findings:
        if item.fingerprint in seen:
            continue
        seen.add(item.fingerprint)
        merged.append(item)
    merged.sort(key=lambda item: (item.source, item.code, item.resource_id or ""))
    truncated = len(merged) > MAX_FINDINGS
    kept = merged[:MAX_FINDINGS]
    counts = {"total": len(kept)}
    for severity in SEVERITIES:
        counts[severity] = sum(1 for item in kept if item.severity == severity)
    counts["unacknowledged"] = len(kept)
    worst = None
    for severity in ("critical", "warning", "info"):
        if counts[severity]:
            worst = severity
            break
    return {
        "drill_run_id": run,
        "forecast_record_id": forecast_record_id,
        "forecast_fingerprint": fingerprint,
        "matches": not kept,
        "forecast_matches": forecast_matches,
        "g_matches": g_matches,
        "worst_severity": worst,
        "findings": [item.as_dict() for item in kept],
        "counts": counts,
        "truncated": truncated,
    }


def miss_penalty(findings):
    """Drift-score penalty (0-100 scale, uncapped) for a finding list.

    ``findings`` are reconcile-report finding dicts. A real miss
    (the drill hit something the forecast never named) costs the
    most; a critical over-prediction costs next; any other warning
    costs least; info findings cost nothing. The weights are
    documented constants, not tuned per tenant.
    """
    if not isinstance(findings, list):
        raise ValueError("findings must be a list.")
    penalty = 0
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("findings must be dicts.")
        if item.get("code") == CODE_DRILLED_NOT_FORECASTED:
            penalty += MISS_POINTS
        elif item.get("severity") == "critical":
            penalty += CRITICAL_POINTS
        elif item.get("severity") == "warning":
            penalty += WARNING_POINTS
    return penalty


def apply_miss_penalty(drift_score, findings):
    """Fold a miss penalty into a stored drift score (raised, never lowered).

    ``drift_score`` is the forecast record's stored score (``None``
    = unknown, treated as 0). The result is capped at 100. A clean
    reconciliation (no findings) leaves the score unchanged.
    """
    if drift_score is not None:
        if isinstance(drift_score, bool) or not isinstance(drift_score, int):
            raise ValueError(f"drift_score must be an int or None, got {drift_score!r}.")
        if not 0 <= drift_score <= 100:
            raise ValueError(f"drift_score out of range: {drift_score!r}.")
    return min(100, (drift_score or 0) + miss_penalty(findings))


def reconciliation_summary(report):
    """One deterministic line for a reconciliation report."""
    if not isinstance(report, dict) or "counts" not in report:
        raise ValueError("report must be a reconcile() result dict.")
    counts = report["counts"]
    run = str(report.get("drill_run_id", ""))[:8]
    fingerprint = str(report.get("forecast_fingerprint", ""))[:8]
    if report.get("matches"):
        return f"reconciliation {run}: match (forecast {fingerprint})"
    return (
        f"reconciliation {run}: {counts['total']} finding(s) - "
        f"{counts['critical']} critical, {counts['warning']} warning, "
        f"{counts['info']} info (forecast {fingerprint})"
    )


__all__ = [
    "CODE_DRILLED_NOT_FORECASTED",
    "CODE_FORECASTED_NOT_DRILLED",
    "CRITICAL_POINTS",
    "G_CODE_SEVERITY",
    "MAX_FINDINGS",
    "MISS_POINTS",
    "SEVERITIES",
    "SOURCES",
    "WARNING_POINTS",
    "ReconciliationFinding",
    "apply_miss_penalty",
    "finding_fingerprint",
    "findings_from_forecast_and_drill",
    "findings_from_g_comparison",
    "miss_penalty",
    "reconcile",
    "reconciliation_summary",
]
