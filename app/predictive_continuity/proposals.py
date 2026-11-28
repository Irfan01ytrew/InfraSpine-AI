"""Forecast-driven proposals (Step L slice 6) — pure rules.

A forecast finding (an uncovered process or a supplier single
point of failure from a slice-1 forecast, or a backup-overlap
warning from the slice-4 calendar) may become an approval-gated
action proposal. Nothing here approves, dispatches, grants
access, or pays anything: this module only decides WHICH
findings may become WHICH proposals, WHO must approve them
(plan §6 safe default), and WHAT exact action the approver sees.

The action itself is a platform :class:`ActionSpec` — bound to
the exact action hash, expiring, single-use, separation of
duties — and is dispatched through ``execute_once`` with
read-back verification by the service layer
(:mod:`app.predictive_continuity.proposals_records`). A proposal
is a suggestion until a human approves it.

Approver classes (plan §6 safe default — the owner decision is
still open, so the mapping is data, not code buried in a route):

* ``owner`` — supplier / money actions. Only the graph owner
  (``company_graph.role.owner``) may approve.
* ``coverage`` — backup actions. A graph approver
  (``company_graph.role.approver`` — the closest existing role
  to a department manager on this branch; when Step J lands the
  department-manager mapping, this class points at the manager
  of the affected process's department) may approve. The graph
  owner holds the approver permission too, through the platform
  mirror (``app.platform.sync``) — the owner role is a superset
  of approver there, so an owner may approve either class.

Money never enters the action params: the finding's money at
risk stays on the finding row (and on the proposal row's
finding summary, redacted for non-owners at read). The action
params carry finding identity only — fingerprint, code, resource,
severity — plus kind-specific string fields. Hostile text in a
finding is data: it is compared and stored as text and can never
grant a permission, change a payee, or change an amount.

The AL what-if engine is NOT on this branch yet (``app.whatif``
does not exist here), so a proposal carries no scenario today.
When AL merges, a proposal may attach a scenario reference —
recorded as owed in the plan (slice 6), never built here as a
second engine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

#: Finding severities (same vocabulary as the calendar warnings).
SEVERITIES = ("info", "warning", "critical")

#: Where a finding came from.
SOURCES = ("forecast", "calendar")

#: Finding kinds (see :func:`findings_from_forecast` and
#: :func:`findings_from_calendar`).
FINDING_KINDS = ("uncovered_process", "supplier_spof", "backup_overlap")

#: Approver classes (plan §6 safe default).
APPROVER_CLASSES = ("owner", "coverage")

#: Platform permission the approver must hold, per class.
REQUIRED_PERMISSION = {
    "owner": "company_graph.role.owner",
    "coverage": "company_graph.role.approver",
}

#: execute_once duty per class (see APPROVAL_REQUIRED in
#: app.platform.access — each duty names its approver permission).
DUTY = {
    "owner": "forecast.apply",
    "coverage": "forecast.apply.coverage",
}

#: Uncovered reasons a slice-1 forecast reports (see
#: ``app.predictive_continuity.forecast``).
UNCOVERED_REASONS = ("no_backup_recorded", "backup_missing_skills", "backup_absent")

#: Due statuses a slice-1 forecast reports.
DUE_STATUSES = ("due_within_horizon", "at_risk_later", "no_deadline")


@dataclass(frozen=True)
class ProposalAction:
    """One kind of action a forecast finding may propose."""

    kind: str
    approver_class: str
    title: str
    description: str

    @property
    def required_permission(self) -> str:
        return REQUIRED_PERMISSION[self.approver_class]

    @property
    def duty(self) -> str:
        return DUTY[self.approver_class]


#: The enumerated action kinds. Nothing outside this table may be
#: proposed from a forecast finding.
PROPOSAL_ACTIONS = {
    "forecast.request_backup": ProposalAction(
        "forecast.request_backup", "coverage",
        "Request backup coverage",
        "Ask for a recorded backup for an uncovered process the "
        "forecast names.",
    ),
    "forecast.request_supplier_review": ProposalAction(
        "forecast.request_supplier_review", "owner",
        "Request supplier review",
        "Ask for a review of a supplier single point of failure "
        "the forecast names.",
    ),
    "forecast.request_backup_review": ProposalAction(
        "forecast.request_backup_review", "coverage",
        "Request backup review",
        "Ask for a review of a backup holder whose leave overlaps "
        "a critical process window.",
    ),
}

#: Which (finding source, finding code) may become which action.
#: Anything not listed here is NOT proposal-eligible — fail-closed:
#: a finding whose code we cannot classify never becomes a proposal.
ELIGIBILITY = {
    ("forecast", "uncovered_process"): ("forecast.request_backup",),
    ("forecast", "supplier_spof"): ("forecast.request_supplier_review",),
    ("calendar", "backup_overlap"): ("forecast.request_backup_review",),
}

_FINGERPRINT_HEX = set("0123456789abcdef")


def _canonical(value):
    """Byte-identical port of ``app.platform.approvals._canonical``.

    This module is stdlib-only (it runs in the worktree venv, which
    has no sqlalchemy), so it cannot import ActionSpec. The port is
    pinned by the DB tests, which build the real ActionSpec from a
    draft's canonical dict and assert the hashes match — if the
    platform's canonical form ever changes, that test fails loudly
    instead of silently diverging.
    """
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        raise ValueError("Use Decimal or strings for amounts and fractional values in an action.")
    raise ValueError(f"Unsupported value type in an action: {type(value).__name__}.")


def _canonical_action(action_type, scope_id, params, *, subject):
    """The canonical ActionSpec dict + its hash, as the platform computes them."""
    canonical = _canonical({
        "type": action_type,
        "scope_id": scope_id,
        "params": params,
        "amount": None,
        "currency": None,
        "subject": subject,
    })
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_str(value, field):
    """A UUID or non-empty string, normalized to ``str``."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{field} must be a non-empty string or UUID, got {value!r}.")


def _check_fingerprint(value, field):
    """A 64-hex fingerprint."""
    text = _as_str(value, field)
    if len(text) != 64 or any(ch not in _FINGERPRINT_HEX for ch in text):
        raise ValueError(f"{field} must be a 64-hex fingerprint, got {text!r}.")
    return text


def _check_uuid(value, field):
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be a UUID, got {value!r}.") from exc
    raise ValueError(f"{field} must be a UUID, got {value!r}.")


def proposal_fingerprint(tenant_id, finding_fingerprint, action_kind):
    """Stable dedupe fingerprint for one proposal.

    SHA-256 over the canonical JSON of (tenant, finding
    fingerprint, action kind), so the same finding proposed for
    the same action is one proposal — a re-proposal returns the
    existing row instead of a duplicate.
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        _check_fingerprint(finding_fingerprint, "finding_fingerprint"),
        action_kind,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def forecast_finding_fingerprint(tenant_id, forecast_fingerprint, kind, resource_id):
    """Stable fingerprint for a finding extracted from a forecast.

    SHA-256 over (tenant, forecast fingerprint, kind, resource),
    so the same forecast yields the same finding identity on
    every run — a re-proposal of the same forecast finding is
    idempotent.
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        _check_fingerprint(forecast_fingerprint, "forecast_fingerprint"),
        kind,
        resource_id or "",
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def calendar_finding_fingerprint(tenant_id, process_id, backup_person_id, overlap_start):
    """Stable fingerprint for a finding from a calendar warning.

    SHA-256 over (tenant, process, backup person, overlap start),
    so the same leave overlap yields the same finding identity on
    every run.
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        process_id or "",
        backup_person_id or "",
        overlap_start or "",
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _check_money(money):
    if money is None:
        return None
    if not isinstance(money, dict) or not money:
        raise ValueError("finding money must be a non-empty dict or null.")
    for currency, amount in money.items():
        if not isinstance(currency, str) or not currency:
            raise ValueError("finding money needs string currency keys.")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise ValueError("finding money amounts must be numbers.")
    return dict(money)


def _check_extra(extra):
    """Extra params must be JSON-safe without floats (like an ActionSpec)."""
    if not isinstance(extra, dict):
        raise ValueError("finding extra must be a dict or null.")
    for key, value in extra.items():
        if not isinstance(key, str) or not key:
            raise ValueError("finding extra keys must be non-empty strings.")
        _check_extra_value(value)
    return dict(extra)


def _check_extra_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        raise ValueError("finding extra must not contain floats (use strings for amounts).")
    if isinstance(value, dict):
        _check_extra(value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_extra_value(item)
        return
    raise ValueError(f"unsupported value type in finding extra: {type(value).__name__}.")


def validate_finding(finding):
    """Normalize + validate a finding dict (either source).

    Returns the normalized finding: ``source``, ``code``,
    ``resource_id`` (or None), ``severity``, ``fingerprint``,
    ``money`` (dict or None), ``detail`` (str), ``extra`` (dict).
    Raises ValueError on anything malformed or unclassifiable.
    """
    if not isinstance(finding, dict):
        raise ValueError("finding must be a dict.")
    source = finding.get("source")
    if source not in SOURCES:
        raise ValueError(f"finding source must be one of {SOURCES}, got {source!r}.")
    code = finding.get("code")
    if not isinstance(code, str) or not code:
        raise ValueError("finding code must be a non-empty string.")
    if code not in FINDING_KINDS:
        raise ValueError(f"unknown finding kind: {code!r}.")
    if (source, code) not in ELIGIBILITY:
        raise ValueError(f"finding {source}:{code} may not become a proposal.")
    resource_id = finding.get("resource_id")
    if resource_id is not None and (not isinstance(resource_id, str) or not resource_id):
        raise ValueError("finding resource_id must be a non-empty string or null.")
    severity = finding.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(f"finding severity must be one of {SEVERITIES}, got {severity!r}.")
    fingerprint = _check_fingerprint(finding.get("fingerprint"), "finding fingerprint")
    money = _check_money(finding.get("money"))
    detail = finding.get("detail")
    if not isinstance(detail, str) or not detail:
        raise ValueError("finding detail must be a non-empty string.")
    extra = _check_extra(finding.get("extra") or {})
    _check_code_extra(source, code, extra)
    return {
        "source": source,
        "code": code,
        "resource_id": resource_id,
        "severity": severity,
        "fingerprint": fingerprint,
        "money": money,
        "detail": detail,
        "extra": extra,
    }


def _check_code_extra(source, code, extra):
    """Kind-specific extra fields a finding must carry."""
    if code == "uncovered_process":
        reason = extra.get("reason")
        if reason not in UNCOVERED_REASONS:
            raise ValueError(f"uncovered_process needs extra.reason in {UNCOVERED_REASONS}, got {reason!r}.")
        if extra.get("due_status") not in DUE_STATUSES:
            raise ValueError(f"uncovered_process needs extra.due_status in {DUE_STATUSES}.")
        if not isinstance(extra.get("horizon_days"), int) or isinstance(extra.get("horizon_days"), bool):
            raise ValueError("uncovered_process needs extra.horizon_days as an int.")
    elif code == "supplier_spof":
        if not isinstance(extra.get("horizon_days"), int) or isinstance(extra.get("horizon_days"), bool):
            raise ValueError("supplier_spof needs extra.horizon_days as an int.")
        if not isinstance(extra.get("processes_due"), int) or isinstance(extra.get("processes_due"), bool):
            raise ValueError("supplier_spof needs extra.processes_due as an int.")
    elif code == "backup_overlap":
        for field in ("process_id", "backup_person_id", "overlap_start", "overlap_end"):
            if not isinstance(extra.get(field), str) or not extra[field]:
                raise ValueError(f"backup_overlap needs extra.{field} as a non-empty string.")
        if not isinstance(extra.get("lead_days"), int) or isinstance(extra.get("lead_days"), bool):
            raise ValueError("backup_overlap needs extra.lead_days as an int.")


def eligible_actions(finding):
    """Which proposal action kinds this finding may become."""
    normalized = validate_finding(finding)
    return list(ELIGIBILITY[(normalized["source"], normalized["code"])])


def draft_proposal(tenant_id, scope_id, finding, action_kind, *, proposed_by):
    """Build the proposal draft for one finding + action kind.

    Returns the draft: proposal fingerprint, the canonical
    :class:`ActionSpec` (+ its hash), the approver class /
    required permission / duty, and the finding summary. The
    draft is pure data — the service stores it and requests the
    platform approval. Money is in the finding summary only,
    never in the action params.
    """
    tenant_uuid = _check_uuid(tenant_id, "tenant_id")
    tenant = str(tenant_uuid)
    _check_uuid(scope_id, "scope_id")
    _check_uuid(proposed_by, "proposed_by")
    normalized = validate_finding(finding)
    if action_kind not in PROPOSAL_ACTIONS:
        raise ValueError(f"unknown proposal action kind: {action_kind!r}.")
    if action_kind not in ELIGIBILITY[(normalized["source"], normalized["code"])]:
        raise ValueError(
            f"finding {normalized['source']}:{normalized['code']} may not become {action_kind}.")
    action = PROPOSAL_ACTIONS[action_kind]
    fingerprint = proposal_fingerprint(tenant, normalized["fingerprint"], action_kind)
    params = {
        "finding_fingerprint": normalized["fingerprint"],
        "finding_code": normalized["code"],
        "finding_source": normalized["source"],
        "resource_id": normalized["resource_id"] or "",
        "severity": normalized["severity"],
        **normalized["extra"],
    }
    subject = f"forecast:{fingerprint}"
    canonical, action_hash = _canonical_action(
        action_kind, UUID(str(scope_id)), params, subject=subject)
    return {
        "proposal_fingerprint": fingerprint,
        "action_kind": action_kind,
        "approver_class": action.approver_class,
        "required_permission": action.required_permission,
        "duty": action.duty,
        "title": action.title,
        "description": action.description,
        "action": canonical,
        "action_hash": action_hash,
        "subject": subject,
        "finding": normalized,
    }


def findings_from_forecast(forecast, *, tenant_id):
    """Extract proposal-eligible findings from a slice-1 forecast dict.

    * Every process with no backup coverage (``reason`` set) is
      an ``uncovered_process`` finding — ``critical`` when the
      process is due within the horizon, ``warning`` otherwise.
      The finding's money is that process's money at risk.
    * Every unavailable supplier root with at least one due
      process is a ``supplier_spof`` finding (``critical``) — the
      supplier is a single point of failure for due work. A
      supplier with no due process proposes nothing: the watch
      rule ``supplier_single_point`` fires on the same
      condition. The finding's money is the forecast's
      due-within-horizon money.

    Returns the findings sorted by (code, resource), deduped by
    fingerprint. A forecast with no uncovered process and no due
    supplier impact yields an empty list — a healthy forecast
    proposes nothing.
    """
    if not isinstance(forecast, dict):
        raise ValueError("forecast must be a slice-1 forecast dict.")
    fingerprint = _check_fingerprint(forecast.get("fingerprint"), "forecast fingerprint")
    processes = forecast.get("processes")
    if not isinstance(processes, list):
        raise ValueError("forecast needs a 'processes' list.")
    unavailable = forecast.get("unavailable")
    if not isinstance(unavailable, list):
        raise ValueError("forecast needs an 'unavailable' list.")
    horizon_days = forecast.get("horizon_days")
    if not isinstance(horizon_days, int) or isinstance(horizon_days, bool):
        raise ValueError("forecast needs 'horizon_days' as an int.")
    tenant = _as_str(tenant_id, "tenant_id")
    due_money = (forecast.get("money_at_risk_due") or {}).get("totals") or None

    findings = []
    seen = set()

    def _add(kind, resource_id, severity, detail, money, extra):
        fp = forecast_finding_fingerprint(tenant, fingerprint, kind, resource_id)
        if fp in seen:
            return
        seen.add(fp)
        findings.append({
            "source": "forecast",
            "code": kind,
            "resource_id": resource_id,
            "severity": severity,
            "fingerprint": fp,
            "money": money,
            "detail": detail,
            "extra": dict(extra),
        })

    for proc in processes:
        if not isinstance(proc, dict):
            raise ValueError("forecast processes must be dicts.")
        reason = proc.get("reason")
        if reason is None:
            continue
        if reason not in UNCOVERED_REASONS:
            raise ValueError(f"unknown uncovered reason: {reason!r}.")
        proc_id = proc.get("id")
        if not isinstance(proc_id, str) or not proc_id:
            raise ValueError("forecast processes need a string id.")
        due_status = proc.get("due_status")
        if due_status not in DUE_STATUSES:
            raise ValueError(f"unknown due_status: {due_status!r}.")
        severity = "critical" if due_status == "due_within_horizon" else "warning"
        money = (proc.get("money_at_risk") or {}).get("totals") or None
        name = proc.get("name") or proc_id
        _add("uncovered_process", proc_id, severity,
             f"forecast: {name} has no backup coverage ({reason})",
             money,
             {"entity_kind": "process", "reason": reason,
              "due_status": due_status, "horizon_days": horizon_days})

    due_processes = [p for p in processes if p.get("due_status") == "due_within_horizon"]
    for root in unavailable:
        if not isinstance(root, dict):
            raise ValueError("forecast unavailable entries must be dicts.")
        if root.get("kind") != "supplier":
            continue
        if not due_processes:
            continue
        root_id = root.get("id")
        if not isinstance(root_id, str) or not root_id:
            raise ValueError("forecast unavailable entries need a string id.")
        name = root.get("name") or root_id
        _add("supplier_spof", root_id, "critical",
             f"forecast: supplier {name} is a single point of failure "
             f"for {len(due_processes)} due process(es)",
             due_money,
             {"entity_kind": "supplier", "horizon_days": horizon_days,
              "processes_due": len(due_processes)})
    findings.sort(key=lambda item: (item["code"], item["resource_id"] or ""))
    return findings


def findings_from_calendar(warnings, *, tenant_id):
    """Extract proposal-eligible findings from calendar warnings.

    Every ``backup_unavailable`` warning from
    :func:`app.predictive_continuity.calendar.backup_overlap_warnings`
    becomes a ``backup_overlap`` finding with the warning's own
    severity (``critical`` when every primary holder is also on
    leave for the whole overlap, otherwise ``warning``). Findings
    carry no money — a leave overlap is a coverage problem, not
    a money figure. Returns the findings sorted by (process,
    backup person), deduped by fingerprint.
    """
    if not isinstance(warnings, list):
        raise ValueError("warnings must be a list of calendar warning dicts.")
    tenant = _as_str(tenant_id, "tenant_id")
    findings = []
    seen = set()
    for warning in warnings:
        if not isinstance(warning, dict):
            raise ValueError("calendar warnings must be dicts.")
        if warning.get("type") != "backup_unavailable":
            raise ValueError(f"unknown calendar warning type: {warning.get('type')!r}.")
        process_id = warning.get("process_id")
        backup_id = warning.get("backup_person_id")
        overlap_start = warning.get("overlap_start")
        overlap_end = warning.get("overlap_end")
        lead_days = warning.get("lead_days")
        severity = warning.get("severity")
        for field, value in (("process_id", process_id), ("backup_person_id", backup_id),
                             ("overlap_start", overlap_start), ("overlap_end", overlap_end)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"calendar warning needs {field} as a non-empty string.")
        if not isinstance(lead_days, int) or isinstance(lead_days, bool):
            raise ValueError("calendar warning needs lead_days as an int.")
        if severity not in SEVERITIES:
            raise ValueError(f"calendar warning severity must be one of {SEVERITIES}, got {severity!r}.")
        fp = calendar_finding_fingerprint(tenant, process_id, backup_id, overlap_start)
        if fp in seen:
            continue
        seen.add(fp)
        findings.append({
            "source": "calendar",
            "code": "backup_overlap",
            "resource_id": process_id,
            "severity": severity,
            "fingerprint": fp,
            "money": None,
            "detail": (
                f"calendar: backup {warning.get('backup_person_name') or backup_id} "
                f"is on leave {overlap_start}-{overlap_end} while process "
                f"{warning.get('process_name') or process_id} needs coverage "
                f"(lead {lead_days}d)"),
            "extra": {
                "process_id": process_id,
                "backup_person_id": backup_id,
                "overlap_start": overlap_start,
                "overlap_end": overlap_end,
                "lead_days": lead_days,
            },
        })
    findings.sort(key=lambda item: (item["resource_id"] or "", item["extra"]["backup_person_id"]))
    return findings


def proposal_summary(draft):
    """One deterministic line for a proposal draft."""
    if not isinstance(draft, dict) or "action_kind" not in draft:
        raise ValueError("draft must be a draft_proposal() result dict.")
    finding = draft["finding"]
    resource = finding["resource_id"] or "—"
    return (
        f"proposal {draft['proposal_fingerprint'][:8]}: {draft['action_kind']} "
        f"({draft['approver_class']} approves) from "
        f"{finding['source']}:{finding['code']} on {resource} "
        f"(action {draft['action_hash'][:8]})"
    )


__all__ = [
    "APPROVER_CLASSES",
    "DUTY",
    "DUE_STATUSES",
    "ELIGIBILITY",
    "FINDING_KINDS",
    "PROPOSAL_ACTIONS",
    "REQUIRED_PERMISSION",
    "SEVERITIES",
    "SOURCES",
    "UNCOVERED_REASONS",
    "ProposalAction",
    "calendar_finding_fingerprint",
    "draft_proposal",
    "eligible_actions",
    "findings_from_calendar",
    "findings_from_forecast",
    "forecast_finding_fingerprint",
    "proposal_fingerprint",
    "proposal_summary",
    "validate_finding",
]
