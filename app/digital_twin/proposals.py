"""Twin-driven proposals (Step K v2 slice 6) — pure rules.

A twin finding (a drill-reconciliation queue row, or a simulation
finding extracted from a :func:`run_whatif` result) may become an
approval-gated action proposal. Nothing here approves, dispatches,
grants access, or pays anything: this module only decides WHICH
findings may become WHICH proposals, WHO must approve them (plan
§6 safe default), and WHAT exact action the approver sees.

The action itself is a platform :class:`ActionSpec` — bound to the
exact action hash, expiring, single-use, separation of duties — and
is dispatched through ``execute_once`` with read-back verification
by the service layer (:mod:`app.digital_twin.proposals_records`).
A proposal is a suggestion until a human approves it.

Approver classes (plan §6 safe default — the owner decision is
still open, so the mapping is data, not code buried in a route):

* ``owner`` — money / privilege / data-restore actions. Only the
  graph owner (``company_graph.role.owner``) may approve.
* ``coverage`` — backup / drill-review actions. A graph approver
  (``company_graph.role.approver`` — the closest existing role to
  a department manager on this branch; when Step J lands the
  department-manager mapping, this class points at the manager of
  the affected process's department) may approve. The graph owner
  holds the approver permission too, through the platform mirror
  (``app.platform.sync``) — the owner role is a superset of
  approver there, so an owner may approve either class.

Money never enters the action params: the finding's money
difference stays on the finding row (and on the proposal row's
finding summary, redacted for non-owners at read). The action
params carry finding identity only — fingerprint, code, resource,
severity — plus kind-specific string fields. Hostile text in a
finding is data: it is compared and stored as text and can never
grant a permission, change a payee, or change an amount.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

#: Finding severities (same vocabulary as the reconciliation queue).
SEVERITIES = ("info", "warning", "critical")

#: Where a finding came from.
SOURCES = ("reconciliation", "simulation")

#: Simulation-finding kinds (see :func:`findings_from_simulation`).
SIMULATION_KINDS = ("uncovered_process", "supplier_spof", "backup_permission_gap")

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
    "owner": "twin.apply",
    "coverage": "twin.apply.coverage",
}

#: Recovery-option actions the twin counts as automated coverage.
#: Anything else (notably ``manual_workaround``) is not coverage:
#: a process whose only recovery is "operate manually" is uncovered.
COVERAGE_ACTIONS = frozenset({"activate_backup", "switch_supplier"})

#: How a what-if entry severity maps onto a finding severity.
#: The engine reports critical/high/medium/low; findings use
#: info/warning/critical. An uncovered failed process is at least a
#: warning — the finding is advisory until a drill proves it.
ENTRY_SEVERITY = {
    "critical": "critical",
    "high": "warning",
    "medium": "warning",
    "low": "info",
}


@dataclass(frozen=True)
class ProposalAction:
    """One kind of action a twin finding may propose."""

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
#: proposed from a twin finding.
PROPOSAL_ACTIONS = {
    "twin.request_backup": ProposalAction(
        "twin.request_backup", "coverage",
        "Request backup coverage",
        "Ask for a recorded backup for an uncovered process or a "
        "drill that missed its recovery objective.",
    ),
    "twin.request_drill_review": ProposalAction(
        "twin.request_drill_review", "coverage",
        "Request drill review",
        "Ask for a review of a simulation/drill disagreement "
        "(affected set, recovery waves, or timing).",
    ),
    "twin.request_supplier_review": ProposalAction(
        "twin.request_supplier_review", "owner",
        "Request supplier review",
        "Ask for a review of a supplier single point of failure.",
    ),
    "twin.request_permission_review": ProposalAction(
        "twin.request_permission_review", "owner",
        "Request permission review",
        "Ask for a review of a backup holder who lacks the "
        "required permission.",
    ),
    "twin.request_money_review": ProposalAction(
        "twin.request_money_review", "owner",
        "Request money review",
        "Ask for a review of a money-at-risk disagreement.",
    ),
    "twin.request_data_review": ProposalAction(
        "twin.request_data_review", "owner",
        "Request data review",
        "Ask for a review of a data-loss disagreement (RPO).",
    ),
}

#: Which (finding source, finding code) may become which action.
#: Anything not listed here is NOT proposal-eligible — fail-closed:
#: a finding whose code we cannot classify never becomes a proposal.
ELIGIBILITY = {
    ("reconciliation", "affected_not_recovered"): ("twin.request_backup",),
    ("reconciliation", "rto_missed_in_drill"): ("twin.request_backup",),
    ("reconciliation", "drill_missed_rto_simulation_met"): ("twin.request_backup",),
    ("reconciliation", "observed_not_simulated"): ("twin.request_drill_review",),
    ("reconciliation", "simulated_not_observed"): ("twin.request_drill_review",),
    ("reconciliation", "recovery_waves_differ"): ("twin.request_drill_review",),
    ("reconciliation", "recovered_not_in_simulation"): ("twin.request_drill_review",),
    ("reconciliation", "drill_met_rto_simulation_missed"): ("twin.request_drill_review",),
    ("reconciliation", "mtpd_exceeded_in_drill"): ("twin.request_drill_review",),
    ("reconciliation", "money_mismatch"): ("twin.request_money_review",),
    ("reconciliation", "data_loss_exceeds_rpo"): ("twin.request_data_review",),
    ("simulation", "uncovered_process"): ("twin.request_backup",),
    ("simulation", "supplier_spof"): ("twin.request_supplier_review",),
    ("simulation", "backup_permission_gap"): ("twin.request_permission_review",),
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


def simulation_finding_fingerprint(tenant_id, simulation_fingerprint, kind, resource_id):
    """Stable fingerprint for a finding extracted from a simulation.

    SHA-256 over (tenant, simulation fingerprint, kind, resource),
    so the same simulation yields the same finding identity on
    every run — a re-proposal of the same simulation finding is
    idempotent.
    """
    payload = [
        _as_str(tenant_id, "tenant_id"),
        _check_fingerprint(simulation_fingerprint, "simulation_fingerprint"),
        kind,
        resource_id or "",
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


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
    if source == "simulation" and code not in SIMULATION_KINDS:
        raise ValueError(f"unknown simulation finding kind: {code!r}.")
    if (source, code) not in ELIGIBILITY:
        raise ValueError(f"finding {source}:{code} may not become a proposal.")
    resource_id = finding.get("resource_id")
    if resource_id is not None and (not isinstance(resource_id, str) or not resource_id):
        raise ValueError("finding resource_id must be a non-empty string or null.")
    severity = finding.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(f"finding severity must be one of {SEVERITIES}, got {severity!r}.")
    fingerprint = _check_fingerprint(finding.get("fingerprint"), "finding fingerprint")
    money = finding.get("money")
    if money is not None:
        if not isinstance(money, dict) or not money:
            raise ValueError("finding money must be a non-empty dict or null.")
        for currency, amount in money.items():
            if not isinstance(currency, str) or not currency:
                raise ValueError("finding money needs string currency keys.")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                raise ValueError("finding money amounts must be numbers.")
    detail = finding.get("detail")
    if not isinstance(detail, str) or not detail:
        raise ValueError("finding detail must be a non-empty string.")
    extra = finding.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("finding extra must be a dict or null.")
    _check_extra(extra)
    return {
        "source": source,
        "code": code,
        "resource_id": resource_id,
        "severity": severity,
        "fingerprint": fingerprint,
        "money": dict(money) if money is not None else None,
        "detail": detail,
        "extra": dict(extra),
    }


def _check_extra(extra):
    """Extra params must be JSON-safe without floats (like an ActionSpec)."""
    for key, value in extra.items():
        if not isinstance(key, str) or not key:
            raise ValueError("finding extra keys must be non-empty strings.")
        _check_extra_value(value)


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


def validate_simulation_finding(finding):
    """Validate a caller-built simulation finding.

    The twin service builds these when it has data the simulation
    result itself does not carry — today only
    ``backup_permission_gap`` (a backup holder who lacks the
    required permission). The finding must name the holder and the
    permission in ``extra``.
    """
    normalized = validate_finding(finding)
    if normalized["source"] != "simulation":
        raise ValueError("a caller-built finding must have source 'simulation'.")
    if normalized["code"] == "backup_permission_gap":
        extra = normalized["extra"]
        holder = extra.get("holder_person_id")
        permission = extra.get("required_permission")
        if not isinstance(holder, str) or not holder:
            raise ValueError("backup_permission_gap needs extra.holder_person_id.")
        if not isinstance(permission, str) or not permission:
            raise ValueError("backup_permission_gap needs extra.required_permission.")
    return normalized


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
    subject = f"twin:{fingerprint}"
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


def map_entry_severity(entry_severity):
    """Map a what-if entry severity onto a finding severity."""
    if not isinstance(entry_severity, str):
        return "warning"
    return ENTRY_SEVERITY.get(entry_severity, "warning")


def findings_from_simulation(simulation, *, tenant_id):
    """Extract proposal-eligible findings from a run_whatif result.

    * Every affected entry in state ``failed`` with no recorded
      backup (``covered_by`` empty) is an ``uncovered_process``
      finding — a process with no one to cover it.
    * In a ``supplier_failure`` scenario, every seed supplier
      whose affected entry has no automated recovery option
      (``activate_backup`` / ``switch_supplier`` — a manual
      workaround is not coverage) is a ``supplier_spof`` finding.
    * ``backup_permission_gap`` findings are NOT extracted here:
      the simulation result does not carry permission data — the
      twin service builds those with
      :func:`validate_simulation_finding` when it has it.

    Returns the findings sorted by (code, resource), deduped by
    fingerprint. A simulation with no uncovered entries yields an
    empty list — a healthy simulation proposes nothing.
    """
    if not isinstance(simulation, dict):
        raise ValueError("simulation must be a run_whatif result dict.")
    affected = simulation.get("affected")
    if not isinstance(affected, list):
        raise ValueError("simulation needs an 'affected' list.")
    scenario = simulation.get("scenario")
    if not isinstance(scenario, dict) or not isinstance(scenario.get("kind"), str):
        raise ValueError("simulation needs a 'scenario' dict with a 'kind'.")
    fingerprint = _check_fingerprint(simulation.get("fingerprint"), "simulation fingerprint")
    tenant = _as_str(tenant_id, "tenant_id")
    scenario_kind = scenario["kind"]
    seed_ids = scenario.get("seed_ids") or []
    if not isinstance(seed_ids, list):
        raise ValueError("simulation scenario seed_ids must be a list.")

    findings = []
    seen = set()

    def _add(kind, resource_id, severity, detail, extra=None):
        fp = simulation_finding_fingerprint(tenant, fingerprint, kind, resource_id)
        if fp in seen:
            return
        seen.add(fp)
        findings.append({
            "source": "simulation",
            "code": kind,
            "resource_id": resource_id,
            "severity": severity,
            "fingerprint": fp,
            "money": None,
            "detail": detail,
            "extra": dict(extra or {}),
        })

    for entry in affected:
        if not isinstance(entry, dict):
            raise ValueError("simulation affected entries must be dicts.")
        entity = entry.get("entity")
        if not isinstance(entity, dict) or not isinstance(entity.get("id"), str) or not entity["id"]:
            raise ValueError("simulation affected entries need an entity with a string id.")
        entity_id = entity["id"]
        covered_by = entry.get("covered_by") or []
        if not isinstance(covered_by, list):
            raise ValueError("simulation affected covered_by must be a list.")
        state = entry.get("state")
        if state == "failed" and not covered_by:
            severity = map_entry_severity(entry.get("severity"))
            name = entity.get("name") or entity_id
            _add("uncovered_process", entity_id, severity,
                 f"simulation: {name} failed with no recorded backup",
                 {"entity_kind": entity.get("kind") or ""})
        if scenario_kind == "supplier_failure" and entity_id in seed_ids:
            options = entry.get("recovery_options") or []
            if not isinstance(options, list):
                raise ValueError("simulation affected recovery_options must be a list.")
            automated = any(
                isinstance(option, dict) and option.get("action") in COVERAGE_ACTIONS
                for option in options
            )
            if not automated:
                severity = map_entry_severity(entry.get("severity"))
                name = entity.get("name") or entity_id
                _add("supplier_spof", entity_id, severity,
                     f"simulation: supplier {name} has no alternative provider",
                     {"entity_kind": entity.get("kind") or ""})
    findings.sort(key=lambda item: (item["code"], item["resource_id"] or ""))
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
    "COVERAGE_ACTIONS",
    "DUTY",
    "ELIGIBILITY",
    "ENTRY_SEVERITY",
    "PROPOSAL_ACTIONS",
    "REQUIRED_PERMISSION",
    "SEVERITIES",
    "SIMULATION_KINDS",
    "SOURCES",
    "ProposalAction",
    "draft_proposal",
    "eligible_actions",
    "findings_from_simulation",
    "map_entry_severity",
    "proposal_fingerprint",
    "proposal_summary",
    "simulation_finding_fingerprint",
    "validate_finding",
    "validate_simulation_finding",
]
