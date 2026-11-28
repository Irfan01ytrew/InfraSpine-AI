"""Standing forecast / always-on watch (Step L slice 2).

Pure functions over a plain-dict twin snapshot. No I/O, no database, no
network, no secrets. Advisory only: a scan or a watch alert is a
warning, never an action — nothing here approves, dispatches, grants
access, restores data, switches a supplier, or pays. Money figures are
exposure ("money at risk"), never payment instructions.

What this slice adds on top of slice 1 (:mod:`forecast`):

- :func:`scan_forecasts` forecasts the unavailability of *every*
  critical entity over the *standard horizons* (1/7/14/30 days) in one
  pass, so a standing forecast exists without anybody remembering to
  ask. The scan is **fingerprint-gated**: when the twin fingerprint
  has not changed since the previous scan, nothing is recomputed and
  nothing re-fires (``refreshed: False``).
- :func:`find_critical_ids` computes the default critical set from
  the snapshot itself, following the plan §6 proposal: systems and
  suppliers with dependents, and the people/teams holding authority
  over a process that has required skills or is linked to money.
  Money entities and processes are not forecastable roots
  (``forecast_unavailability`` only accepts person/team/system/
  supplier), so they make their *holders* critical instead.
- :func:`evaluate_forecast_watch` evaluates **rules as data** against
  a scan result, following the Step AH watch pattern
  (``app/resilience/watch.py``): SHA-256 dedupe fingerprint per
  (rule, root, horizon), cooldown windows, and quiet hours
  22:00–07:00 Asia/Karachi where only rules with
  ``breaks_quiet_hours`` fire and the rest are reported as
  ``deferred`` (never silently dropped). A finding is a suggestion
  until a human approves it (slice 6 wires proposals); the watch
  never dispatches anything itself.

Visibility: ``visible_ids`` is passed through to the slice-1
forecast, so hidden processes are omitted from every list, count and
money total. Alert detail carries counts and money totals only — no
process names — and a hidden unavailable root is named by its id,
exactly as slice 1 reports it.

Snapshot text is untrusted data. Names, attributes and due dates are
compared as data only; the watch never ``eval``s, ``exec``s, or
interprets text as an instruction. Amounts come only from numeric
``amount`` attributes, validated by slice 1.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.predictive_continuity.forecast import (
    AUTHORITY_KINDS,
    DEFAULT_DEPTH,
    MONEY_KINDS,
    STANDARD_HORIZONS,
    UNAVAILABLE_KINDS,
    _validate_snapshot,
    forecast_unavailability,
    snapshot_fingerprint,
)

ORG_TZ = ZoneInfo("Asia/Karachi")

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)

# Bounds: a scan is critical_ids x horizons forecast passes over the
# whole twin. Refuse absurd inputs instead of grinding the VM.
MAX_CRITICAL_IDS = 200
MAX_HORIZONS = 8

SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class ForecastRule:
    """One standing-forecast watch rule. Rules are data — thresholds,
    cooldowns and quiet-hour behaviour are configured, never hard-coded
    in the evaluator.
    """

    key: str
    severity: str  # "critical" | "warning"
    title: str
    # May contain {name}, {kind}, {horizon}, {processes}, {due},
    # {uncovered} and {money} placeholders, filled at fire time.
    detail: str
    min_processes_due: int = 0  # fire when a forecast has >= this many processes due within horizon
    min_uncovered: int = 0  # fire when a forecast has >= this many uncovered processes
    kinds: frozenset[str] | None = None  # fire only when the unavailable root is one of these kinds
    horizons: frozenset[int] | None = None  # fire only for these horizon lengths (days)
    cooldown_seconds: int = 3600
    breaks_quiet_hours: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class ForecastAlert:
    rule_key: str
    severity: str
    root_id: str
    root_name: str
    root_kind: str
    horizon_days: int
    processes: int
    processes_due: int
    uncovered: int
    # Sorted (currency, amount) pairs of money due within the horizon.
    money_due: tuple[tuple[str, float], ...]
    fingerprint: str
    evaluated_at: datetime
    title: str
    detail: str


DEFAULT_RULES: tuple[ForecastRule, ...] = (
    ForecastRule(
        key="forecast_critical",
        severity="critical",
        title="Critical process uncovered within the forecast horizon",
        detail="{name} ({kind}) unavailable for {horizon} days leaves {due} process(es) due "
               "and {uncovered} uncovered. Money at risk due: {money}.",
        min_processes_due=1,
        min_uncovered=1,
        cooldown_seconds=3600,
        breaks_quiet_hours=True,
    ),
    ForecastRule(
        key="forecast_due",
        severity="warning",
        title="Several processes due within the forecast horizon",
        detail="{name} ({kind}) unavailable for {horizon} days leaves {due} process(es) due "
               "within the horizon ({processes} affected). Money at risk due: {money}.",
        min_processes_due=3,
        cooldown_seconds=7200,
    ),
    ForecastRule(
        key="forecast_uncovered",
        severity="warning",
        title="Process has no usable backup in the forecast",
        detail="{name} ({kind}) unavailable for {horizon} days leaves {uncovered} "
               "process(es) without a usable backup. Money at risk due: {money}.",
        min_uncovered=1,
        cooldown_seconds=14400,
    ),
    ForecastRule(
        key="supplier_single_point",
        severity="warning",
        title="Supplier failure breaks a process",
        detail="{name} (supplier) unavailable for {horizon} days breaks {processes} "
               "process(es), {due} of them due within the horizon. "
               "Money at risk due: {money}.",
        min_processes_due=1,
        kinds=frozenset({"supplier"}),
        cooldown_seconds=14400,
    ),
)


def watch_fingerprint(rule_key: str, root_id: str, horizon_days: int) -> str:
    """Stable dedupe identity for one (rule, root, horizon) firing."""
    return hashlib.sha256(
        f"{rule_key}:{root_id}:{horizon_days}".encode("utf-8")
    ).hexdigest()


def validate_forecast_rules(rules) -> list[str]:
    """Return a list of problems with a rule set (empty means valid)."""
    problems: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule.key, str) or not rule.key:
            problems.append("rule key must be a non-empty string")
            continue
        if rule.key in seen:
            problems.append(f"duplicate rule key: {rule.key}")
        seen.add(rule.key)
        if rule.severity not in SEVERITIES:
            problems.append(f"rule {rule.key}: bad severity {rule.severity!r}")
        if not isinstance(rule.title, str) or not rule.title:
            problems.append(f"rule {rule.key}: title must be a non-empty string")
        if not isinstance(rule.detail, str) or not rule.detail:
            problems.append(f"rule {rule.key}: detail must be a non-empty string")
        for field in ("min_processes_due", "min_uncovered"):
            value = getattr(rule, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"rule {rule.key}: {field} must be a non-negative integer")
        if isinstance(rule.cooldown_seconds, bool) or not isinstance(rule.cooldown_seconds, int) \
                or rule.cooldown_seconds <= 0:
            problems.append(f"rule {rule.key}: cooldown must be a positive integer")
        if rule.kinds is not None and (not rule.kinds or not all(
                isinstance(kind, str) and kind for kind in rule.kinds)):
            problems.append(f"rule {rule.key}: kinds must not be empty")
        if rule.horizons is not None and (not rule.horizons or not all(
                isinstance(h, int) and not isinstance(h, bool) and h >= 1
                for h in rule.horizons)):
            problems.append(f"rule {rule.key}: horizons must be positive integers")
    return problems


def is_quiet_hour(
    now: datetime,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> bool:
    """True when ``now`` falls inside the quiet window.

    Handles overnight windows (start > end, e.g. 22:00–07:00) as well as
    same-day windows. A naive datetime is read as org time
    (Asia/Karachi) — the same convention as the Step AH watch.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    current = now.timetz().replace(tzinfo=None)
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def fingerprint_changed(previous_fingerprint, snapshot) -> bool:
    """True when the twin fingerprint differs from ``previous_fingerprint``.

    ``previous_fingerprint`` of None means "never scanned" — the twin
    has always changed. Mirrors the Step S horizon helper of the same
    name.
    """
    if previous_fingerprint is None:
        return True
    if not isinstance(previous_fingerprint, str) or not previous_fingerprint:
        raise ValueError("previous_fingerprint must be None or a non-empty string.")
    return snapshot_fingerprint(snapshot) != previous_fingerprint


def find_critical_ids(snapshot) -> list[str]:
    """Default critical set for the standing scan (plan §6 proposal).

    A forecastable root (person/team/system/supplier) is critical when:

    - it is a system or supplier with dependents — something
      ``depends_on`` it, it ``supplies`` something, or it holds
      authority (``owns`` / ``responsible_for`` / ``operates`` /
      ``approves``) over something; or
    - it is a person or team holding authority over a process that
      has required skills, or over a process linked to a money
      entity (invoice batch / payment).

    Processes and money entities are not forecastable roots, so they
    make their holders critical instead. Returns sorted ids.
    """
    nodes = _validate_snapshot(snapshot)
    edges = snapshot.get("relationships", [])
    critical: set[str] = set()

    dependents: set[str] = set()
    for edge in edges:
        kind = edge["kind"]
        if kind == "depends_on":
            dependents.add(edge["target_id"])
        elif kind == "supplies":
            dependents.add(edge["source_id"])
        elif kind in AUTHORITY_KINDS:
            dependents.add(edge["source_id"])
    for entity_id in dependents:
        if nodes[entity_id]["kind"] in ("system", "supplier"):
            critical.add(entity_id)

    money_linked_processes: set[str] = set()
    for edge in edges:
        source, target = edge["source_id"], edge["target_id"]
        if nodes[source]["kind"] in MONEY_KINDS and nodes[target]["kind"] == "process":
            money_linked_processes.add(target)
        if nodes[target]["kind"] in MONEY_KINDS and nodes[source]["kind"] == "process":
            money_linked_processes.add(source)

    for edge in edges:
        if edge["kind"] not in AUTHORITY_KINDS:
            continue
        holder, target = edge["source_id"], edge["target_id"]
        if nodes[holder]["kind"] not in ("person", "team"):
            continue
        if nodes[target]["kind"] != "process":
            continue
        attributes = nodes[target].get("attributes", {}) or {}
        required = attributes.get("required_skills") or []
        if required or target in money_linked_processes:
            critical.add(holder)
    return sorted(critical)


def _as_org_time(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=ORG_TZ)


def _as_utc(moment: datetime) -> datetime:
    return _as_org_time(moment).astimezone(UTC)


def _validate_scan_inputs(snapshot, critical_ids, horizons):
    if critical_ids is None:
        critical_ids = find_critical_ids(snapshot)
    else:
        if not isinstance(critical_ids, (list, tuple, set, frozenset)):
            raise ValueError("critical_ids must be None or a list/set of entity ids.")
        as_list = list(critical_ids)
        if not as_list:
            raise ValueError("critical_ids must not be empty.")
        if len(set(as_list)) != len(as_list):
            raise ValueError("critical_ids must not contain duplicates.")
        for entity_id in as_list:
            if not isinstance(entity_id, str) or not entity_id:
                raise ValueError("critical_ids must contain non-empty strings only.")
        critical_ids = sorted(as_list)
    if len(critical_ids) > MAX_CRITICAL_IDS:
        raise ValueError(
            f"scan covers {len(critical_ids)} critical entities, over the {MAX_CRITICAL_IDS} cap."
        )
    if horizons is None:
        horizons = list(STANDARD_HORIZONS)
    else:
        if not isinstance(horizons, (list, tuple, set, frozenset)):
            raise ValueError("horizons must be None or a list/set of day counts.")
        horizons = list(horizons)
        if not horizons:
            raise ValueError("horizons must not be empty.")
        if len(set(horizons)) != len(horizons):
            raise ValueError("horizons must not contain duplicates.")
        for horizon in horizons:
            if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
                raise ValueError("horizons must contain positive integers (days) only.")
        if len(horizons) > MAX_HORIZONS:
            raise ValueError(f"scan covers {len(horizons)} horizons, over the {MAX_HORIZONS} cap.")
        horizons = sorted(horizons)
    nodes = _validate_snapshot(snapshot)
    for entity_id in critical_ids:
        if entity_id not in nodes:
            raise ValueError(f"Unknown critical id in snapshot: {entity_id!r}.")
        if nodes[entity_id]["kind"] not in UNAVAILABLE_KINDS:
            raise ValueError(
                f"Cannot forecast unavailability of kind {nodes[entity_id]['kind']!r} "
                f"({entity_id!r}); only person, team, system and supplier may be unavailable."
            )
    return critical_ids, horizons


def scan_forecasts(snapshot, critical_ids=None, *, horizons=None,
                   now=None, visible_ids=None, previous_fingerprint=None,
                   depth=DEFAULT_DEPTH) -> dict:
    """Standing forecast: every critical entity x every standard horizon.

    ``critical_ids`` defaults to :func:`find_critical_ids` (the plan §6
    proposal). ``horizons`` defaults to ``STANDARD_HORIZONS``
    (1/7/14/30 days). ``now`` is the scan moment — a naive datetime is
    read as org time (Asia/Karachi) and converted to UTC for the
    forecast, matching the watch's quiet-hour convention.
    ``visible_ids`` is passed through to the slice-1 forecast.
    ``previous_fingerprint`` is the fingerprint of the last scan: when
    it matches the current twin fingerprint the scan returns
    ``refreshed: False`` with no forecasts — an unchanged twin never
    re-fires. The caller keeps the last scan result.

    Returns ``{"fingerprint", "refreshed", "scanned_at", "horizons",
    "critical_ids", "forecasts", "counts"}``. Each forecast entry
    carries ``root_id`` / ``root_kind`` / ``root_name`` /
    ``horizon_days`` / ``processes`` / ``processes_due`` /
    ``uncovered`` / ``money_due`` (sorted (currency, amount) pairs)
    plus the full slice-1 ``forecast`` dict. Entries are sorted by
    (root id, horizon), so the scan is deterministic.
    """
    moment = _as_utc(now) if now is not None else datetime.now(UTC)
    if not isinstance(moment, datetime):
        raise ValueError("now must be a datetime.")
    if previous_fingerprint is not None and (
            not isinstance(previous_fingerprint, str) or not previous_fingerprint):
        raise ValueError("previous_fingerprint must be None or a non-empty string.")

    critical_ids, horizons = _validate_scan_inputs(snapshot, critical_ids, horizons)
    fingerprint = snapshot_fingerprint(snapshot)

    counts = {
        "critical": len(critical_ids),
        "horizons": len(horizons),
        "forecasts": 0,
        "with_due_processes": 0,
        "with_uncovered": 0,
    }
    base = {
        "fingerprint": fingerprint,
        "scanned_at": moment.isoformat(),
        "horizons": list(horizons),
        "critical_ids": list(critical_ids),
        "forecasts": [],
        "counts": counts,
    }
    if previous_fingerprint is not None and previous_fingerprint == fingerprint:
        return {**base, "refreshed": False}
    forecasts = []
    for root_id in critical_ids:
        for horizon_days in horizons:
            forecast = forecast_unavailability(
                snapshot, [root_id], horizon_days=horizon_days,
                now=moment, visible_ids=visible_ids, depth=depth,
            )
            due = forecast["counts"]["processes_due_within_horizon"]
            uncovered = forecast["counts"]["uncovered"]
            money_due = tuple(sorted(forecast["money_at_risk_due"]["totals"].items()))
            forecasts.append({
                "root_id": root_id,
                "root_kind": forecast["unavailable"][0]["kind"],
                "root_name": forecast["unavailable"][0]["name"],
                "horizon_days": horizon_days,
                "processes": forecast["counts"]["processes"],
                "processes_due": due,
                "uncovered": uncovered,
                "money_due": money_due,
                "forecast": forecast,
            })
            counts["forecasts"] += 1
            if due:
                counts["with_due_processes"] += 1
            if uncovered:
                counts["with_uncovered"] += 1
    return {**base, "refreshed": True, "forecasts": forecasts}


def _format_money(money_due) -> str:
    if not money_due:
        return "none"
    return ", ".join(f"{currency} {amount}" for currency, amount in money_due)


def _rule_matches(rule: ForecastRule, entry: dict) -> bool:
    if entry["processes_due"] < rule.min_processes_due:
        return False
    if entry["uncovered"] < rule.min_uncovered:
        return False
    if rule.kinds is not None and entry["root_kind"] not in rule.kinds:
        return False
    if rule.horizons is not None and entry["horizon_days"] not in rule.horizons:
        return False
    return True


def evaluate_forecast_watch(scan_result, *, now: datetime,
                            rules=DEFAULT_RULES,
                            sent_at: dict[str, datetime] | None = None,
                            quiet_start: time = DEFAULT_QUIET_START,
                            quiet_end: time = DEFAULT_QUIET_END) -> dict:
    """Evaluate every enabled rule against a :func:`scan_forecasts` result.

    ``sent_at`` maps alert fingerprint -> when that (rule, root,
    horizon) pair last fired; a pair inside its cooldown window is
    reported under ``suppressed`` instead of firing again. During
    quiet hours only rules with ``breaks_quiet_hours`` fire; the rest
    are reported under ``deferred``. Output is critical-first, then
    most processes due, then most uncovered, then root id, then
    horizon — deterministic.

    Returns ``{"alerts", "suppressed", "deferred", "evaluated_at",
    "fingerprint"}``. Suppressed/deferred entries carry the same
    fields as an alert plus a ``reason`` (``"cooldown"`` /
    ``"quiet_hours"``). Alerts are advisory only: nothing here acts
    on them.
    """
    problems = validate_forecast_rules(rules)
    if problems:
        raise ValueError("Invalid forecast-watch rules: " + "; ".join(problems))
    if not isinstance(scan_result, dict) or not isinstance(scan_result.get("forecasts"), list):
        raise ValueError("scan_result must be a scan_forecasts result dict.")
    now = _as_org_time(now)
    sent_at = sent_at or {}
    quiet = is_quiet_hour(now, quiet_start, quiet_end)

    fired: list[ForecastAlert] = []
    suppressed: list[dict] = []
    deferred: list[dict] = []
    for entry in scan_result["forecasts"]:
        for rule in rules:
            if not rule.enabled:
                continue
            if not _rule_matches(rule, entry):
                continue
            fp = watch_fingerprint(rule.key, entry["root_id"], entry["horizon_days"])
            detail = rule.detail.format(
                name=entry["root_name"],
                kind=entry["root_kind"],
                horizon=entry["horizon_days"],
                processes=entry["processes"],
                due=entry["processes_due"],
                uncovered=entry["uncovered"],
                money=_format_money(entry["money_due"]),
            )
            record = {
                "rule_key": rule.key,
                "severity": rule.severity,
                "root_id": entry["root_id"],
                "root_name": entry["root_name"],
                "root_kind": entry["root_kind"],
                "horizon_days": entry["horizon_days"],
                "processes": entry["processes"],
                "processes_due": entry["processes_due"],
                "uncovered": entry["uncovered"],
                "money_due": entry["money_due"],
                "fingerprint": fp,
                "evaluated_at": now,
                "title": rule.title,
                "detail": detail,
            }
            last_sent = sent_at.get(fp)
            if last_sent is not None:
                sent = _as_org_time(last_sent)
                if (now - sent) < timedelta(seconds=rule.cooldown_seconds):
                    suppressed.append({**record, "reason": "cooldown"})
                    continue
            if quiet and not rule.breaks_quiet_hours:
                deferred.append({**record, "reason": "quiet_hours"})
                continue
            fired.append(ForecastAlert(**record))

    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    fired.sort(
        key=lambda alert: (
            order.get(alert.severity, len(order)),
            -alert.processes_due,
            -alert.uncovered,
            alert.root_id,
            alert.horizon_days,
        )
    )
    return {
        "alerts": fired,
        "suppressed": suppressed,
        "deferred": deferred,
        "evaluated_at": now,
        "fingerprint": scan_result.get("fingerprint"),
    }


def format_watch_message(alert: ForecastAlert) -> str:
    """Plain-language message for in-app / WhatsApp / email delivery."""
    return (
        f"[InfraSpine • predictive continuity • {alert.severity}] {alert.title}: "
        f"{alert.root_name} unavailable {alert.horizon_days} days — {alert.detail}"
    )


__all__ = [
    "DEFAULT_QUIET_END",
    "DEFAULT_QUIET_START",
    "DEFAULT_RULES",
    "MAX_CRITICAL_IDS",
    "MAX_HORIZONS",
    "ORG_TZ",
    "ForecastAlert",
    "ForecastRule",
    "evaluate_forecast_watch",
    "find_critical_ids",
    "fingerprint_changed",
    "format_watch_message",
    "is_quiet_hour",
    "scan_forecasts",
    "validate_forecast_rules",
    "watch_fingerprint",
]
