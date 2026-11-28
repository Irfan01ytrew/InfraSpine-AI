"""Drill reconciliation loop (Step K v2 slice 5).

Pure functions; stdlib only, except the twin's own ``compare_drill``
(:mod:`app.digital_twin.whatif`) and the drift fingerprint. No imports
from ``app.db``, no I/O, no network, no secrets, and no import from
``app.continuity`` — Step G is not merged on this branch yet, so the
Step G side arrives as the plain dict that Step G's
``drill_comparison`` returns (see ``app/continuity/drills.py`` on the
``agent/stepg-continuity`` branch). When G merges, the caller passes
that dict straight in; nothing here needs to change.

The problem this solves: the twin and Step G compare a simulation
against a drill in two different vocabularies.

* The twin's :func:`app.digital_twin.whatif.compare_drill` compares a
  ``run_whatif`` result with a drill observation in twin shape
  (``observed_affected`` / ``observed_money_at_risk`` /
  ``observed_recovery_waves``) and reports affected-set, money-total,
  and recovery-wave differences as plain strings.
* Step G's ``compare_drill_to_simulation`` compares a drill run with
  a simulation forecast on timing (RTO / MTPD / RPO per resource) and
  reports differences as ``{code, resource_id, detail}`` dicts.

Neither side sees the other's differences. The reconciliation loop
runs both comparisons and unifies them into ONE review-queue finding
format: ``{code, resource_id, severity, source, detail,
fingerprint, money}``. Differences go to the review queue
(:mod:`app.digital_twin.reconciliation_records`) — they are never
auto-applied, and the loop never rewrites a drill record or a twin
version. A finding is a fact about one drill run vs one simulation;
acknowledging it only marks the queue row as seen.

Money: a money-mismatch finding carries the per-currency diff in its
``money`` field (exposure totals only, never an account number) and
its ``detail`` text carries NO amount — the API redacts ``money``
for non-owners, the same owner-only rule as the drift endpoint
(plan §4). Finding detail otherwise carries entity ids and timing
minutes only — no names, no secrets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from app.digital_twin.whatif import compare_drill

#: Finding severities, worst first.
SEVERITIES = ("info", "warning", "critical")

#: Which side of the comparison produced the finding.
SOURCES = ("twin", "continuity")

#: Hard cap on findings in one reconciliation report, so a wild
#: drill cannot flood the review queue. Reports that hit the cap
#: say so (``truncated: True``) instead of silently dropping rows.
MAX_FINDINGS = 500

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

#: Twin-side finding codes, one per kind of difference the twin
#: comparison can report.
TWIN_CODE_SIMULATED_NOT_OBSERVED = "simulated_not_observed"
TWIN_CODE_OBSERVED_NOT_SIMULATED = "observed_not_simulated"
TWIN_CODE_MONEY_MISMATCH = "money_mismatch"
TWIN_CODE_WAVES_DIFFER = "recovery_waves_differ"

_FINGERPRINT_HEX = set("0123456789abcdef")


def _as_str(value, field):
    """A UUID or non-empty string, normalized to ``str``."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{field} must be a non-empty string or UUID, got {value!r}.")


def _check_fingerprint(value, field):
    """A 64-hex twin fingerprint (the drift/horizon fingerprint)."""
    text = _as_str(value, field)
    if len(text) != 64 or any(ch not in _FINGERPRINT_HEX for ch in text):
        raise ValueError(f"{field} must be a 64-hex fingerprint, got {text!r}.")
    return text


def finding_fingerprint(tenant_id, drill_run_id, simulation_fingerprint, code, resource_id,
                        money=None):
    """Stable dedupe fingerprint for one finding.

    SHA-256 over the canonical JSON of
    ``(tenant, drill run, simulation fingerprint, code, resource,
    money)``, so the same finding re-reconciled for the same drill
    run and the same simulation is stored once. Two different
    simulations of the same drill run produce different fingerprints
    and both stay in the queue — and two money findings for
    different currencies are different findings, not one row
    (``money`` is part of the identity for that reason).
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        _as_str(drill_run_id, "drill_run_id"),
        _check_fingerprint(simulation_fingerprint, "simulation_fingerprint"),
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


def _finding(tenant_id, drill_run_id, simulation_fingerprint, *, code, resource_id,
             severity, source, detail, money=None):
    return ReconciliationFinding(
        code=code,
        resource_id=resource_id,
        severity=severity,
        source=source,
        detail=detail,
        fingerprint=finding_fingerprint(
            tenant_id, drill_run_id, simulation_fingerprint, code, resource_id, money),
        money=money,
    )


def findings_from_twin_comparison(comparison, *, tenant_id, drill_run_id, simulation_fingerprint):
    """Adapt a twin :func:`compare_drill` result into findings.

    One finding per affected-set difference, one per currency with
    a money difference, one when the recovery waves disagree. The
    ``detail`` text carries no money amount — the amount lives in
    the finding's ``money`` field, which the API redacts for
    non-owners.
    """
    if not isinstance(comparison, dict):
        raise ValueError("twin comparison must be a dict.")
    for field in ("simulated_only", "observed_only", "money_difference", "wave_match"):
        if field not in comparison:
            raise ValueError(f"twin comparison needs {field!r}.")
    tenant = _as_str(tenant_id, "tenant_id")
    run = _as_str(drill_run_id, "drill_run_id")
    fingerprint = _check_fingerprint(simulation_fingerprint, "simulation_fingerprint")
    findings = []
    for entity_id in sorted(comparison["simulated_only"]):
        findings.append(_finding(
            tenant, run, fingerprint,
            code=TWIN_CODE_SIMULATED_NOT_OBSERVED, resource_id=str(entity_id),
            severity="warning", source="twin",
            detail=f"simulation affected {entity_id} but the drill did not"))
    for entity_id in sorted(comparison["observed_only"]):
        findings.append(_finding(
            tenant, run, fingerprint,
            code=TWIN_CODE_OBSERVED_NOT_SIMULATED, resource_id=str(entity_id),
            severity="warning", source="twin",
            detail=f"drill affected {entity_id} but the simulation did not"))
    money_difference = comparison["money_difference"]
    if not isinstance(money_difference, dict):
        raise ValueError("twin comparison money_difference must be a dict.")
    for currency in sorted(money_difference):
        findings.append(_finding(
            tenant, run, fingerprint,
            code=TWIN_CODE_MONEY_MISMATCH, resource_id=None,
            severity="warning", source="twin",
            detail=f"money at risk differs for {currency}",
            money={currency: money_difference[currency]}))
    if not comparison["wave_match"]:
        findings.append(_finding(
            tenant, run, fingerprint,
            code=TWIN_CODE_WAVES_DIFFER, resource_id=None,
            severity="warning", source="twin",
            detail="recovery waves differ between the simulation and the drill"))
    findings.sort(key=lambda item: (item.code, item.resource_id or ""))
    return findings


def findings_from_g_comparison(g_comparison, *, tenant_id, drill_run_id, simulation_fingerprint):
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
    fingerprint = _check_fingerprint(simulation_fingerprint, "simulation_fingerprint")
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


def reconcile(simulation, drill_observation, *, tenant_id, drill_run_id,
              simulation_fingerprint=None, g_comparison=None, scenario_kind=None):
    """Reconcile one twin simulation against one real Step G drill.

    Runs the twin comparison (:func:`compare_drill`) and, when a
    Step G comparison is supplied, adapts its timing differences
    too, then merges both sides into one deterministic finding
    list, deduped by fingerprint. ``matches`` is True only when
    neither side reports any difference.

    ``simulation`` is a :func:`run_whatif` result;
    ``drill_observation`` is a twin-shape drill dict (see
    :func:`compare_drill`); ``g_comparison`` is the plain dict
    Step G's ``drill_comparison`` returns. The simulation
    fingerprint comes from the simulation itself or the explicit
    argument — a reconciliation without one is a ``ValueError``,
    because findings must be tied to the twin version they were
    computed from. Nothing here writes anything: the caller stores
    the report through
    :func:`app.digital_twin.reconciliation_records.record_reconciliation`.
    """
    tenant = _as_str(tenant_id, "tenant_id")
    run = _as_str(drill_run_id, "drill_run_id")
    fingerprint = simulation_fingerprint
    if fingerprint is None and isinstance(simulation, dict):
        fingerprint = simulation.get("fingerprint")
    fingerprint = _check_fingerprint(fingerprint, "simulation_fingerprint")
    if scenario_kind is not None and (not isinstance(scenario_kind, str) or not scenario_kind):
        raise ValueError("scenario_kind must be a non-empty string or null.")
    twin_comparison = compare_drill(simulation, drill_observation)
    findings = findings_from_twin_comparison(
        twin_comparison, tenant_id=tenant, drill_run_id=run,
        simulation_fingerprint=fingerprint)
    g_matches = None
    if g_comparison is not None:
        g_findings = findings_from_g_comparison(
            g_comparison, tenant_id=tenant, drill_run_id=run,
            simulation_fingerprint=fingerprint)
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
    return {
        "drill_run_id": run,
        "drill_id": twin_comparison.get("drill_id"),
        "scenario_kind": scenario_kind,
        "simulation_fingerprint": fingerprint,
        "matches": not kept,
        "twin_matches": bool(twin_comparison.get("matches")),
        "g_matches": g_matches,
        "findings": [item.as_dict() for item in kept],
        "counts": counts,
        "truncated": truncated,
    }


def reconciliation_summary(report):
    """One deterministic line for a reconciliation report."""
    if not isinstance(report, dict) or "counts" not in report:
        raise ValueError("report must be a reconcile() result dict.")
    counts = report["counts"]
    run = str(report.get("drill_run_id", ""))[:8]
    fingerprint = str(report.get("simulation_fingerprint", ""))[:8]
    if report.get("matches"):
        return f"reconciliation {run}: match (simulation {fingerprint})"
    return (
        f"reconciliation {run}: {counts['total']} finding(s) — "
        f"{counts['critical']} critical, {counts['warning']} warning, "
        f"{counts['info']} info (simulation {fingerprint})"
    )


__all__ = [
    "G_CODE_SEVERITY",
    "MAX_FINDINGS",
    "ReconciliationFinding",
    "SEVERITIES",
    "SOURCES",
    "TWIN_CODE_MONEY_MISMATCH",
    "TWIN_CODE_OBSERVED_NOT_SIMULATED",
    "TWIN_CODE_SIMULATED_NOT_OBSERVED",
    "TWIN_CODE_WAVES_DIFFER",
    "finding_fingerprint",
    "findings_from_g_comparison",
    "findings_from_twin_comparison",
    "reconcile",
    "reconciliation_summary",
]
