"""Horizon predictions + watch (Step S slice 4).

Pure functions over a twin snapshot. No I/O, no database, no network,
no secrets. Advisory only: a prediction or a watch alert is a
notification, never an action — nothing here approves, dispatches,
restores data, switches a supplier, or pays. Alert text carries node
names and risk scores only; money figures are exposure totals the twin
already shows, never account numbers or payment instructions.

What this slice adds on top of slice 1 (``app.digital_twin.impact``):

- :func:`predict_absence` — "unavailable N days" for one absence set,
  wrapped with the snapshot fingerprint it was computed from, a
  0–100 risk score, and explainable factors, so a caller can tell
  whether a prediction is still current.
- :func:`fingerprint_changed` / :func:`refresh_predictions` — the
  watch refreshes predictions only when the twin fingerprint moved
  (a backup link removed, a permission changed, a batch added), so
  an unchanged twin never re-fires.
- :func:`evaluate_horizon_watch` — rules as data over the predictions
  (mirroring the Step AH risk-watch pattern in
  ``app.resilience.watch``): severity thresholds on the risk score,
  SHA-256 dedupe with cooldown, quiet hours 22:00–07:00 Asia/Karachi
  where only critical rules break through, and the rest reported as
  ``deferred`` so nothing is silently dropped.

Risk score (0–100, documented weights, deterministic):

- uncovered entities (at risk with no usable backup): 15 points each,
  capped at 45;
- affected entities: 2 points each, capped at 30;
- money at risk: up to 25 points — the largest per-currency share of
  the snapshot's total money at risk (0 when the snapshot holds no
  money, or none of it is at risk).

The score is an advisory ranking, not a probability of failure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.digital_twin.impact import money_at_risk, simulate_absence

ORG_TZ = ZoneInfo("Asia/Karachi")

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)

SEVERITIES = ("critical", "warning")

_UNCOVERED_POINTS = 15
_UNCOVERED_CAP = 45
_AFFECTED_POINTS = 2
_AFFECTED_CAP = 30
_MONEY_POINTS = 25


def snapshot_fingerprint(snapshot):
    """Fingerprint of a twin snapshot.

    Uses the fingerprint the snapshot builder stored when present
    (``app.digital_twin.snapshot.build_snapshot``); otherwise computes
    the same canonical hash (SHA-256 over the sorted JSON of entities
    + relationships) so a hand-built snapshot still has a stable id.
    """
    stored = snapshot.get("fingerprint") if isinstance(snapshot, dict) else None
    if isinstance(stored, str) and stored:
        return stored
    canonical = json.dumps(
        {"entities": snapshot.get("entities", []),
         "relationships": snapshot.get("relationships", [])},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_changed(old_fingerprint, snapshot):
    """True when the twin moved since ``old_fingerprint``.

    ``old_fingerprint`` of None always counts as changed (first run).
    """
    return old_fingerprint != snapshot_fingerprint(snapshot)


def _risk_score(simulation, snapshot):
    """0–100 advisory score + explainable factors for one simulation."""
    affected_count = len(simulation["affected"])
    uncovered_count = len(simulation["uncovered"])
    blocked_count = len(simulation["blocked_by_dependency_cycle"])
    at_risk = simulation["money_at_risk"]["totals"]
    total_money = money_at_risk(snapshot.get("entities", []))["totals"]

    uncovered_points = min(_UNCOVERED_CAP, _UNCOVERED_POINTS * uncovered_count)
    affected_points = min(_AFFECTED_CAP, _AFFECTED_POINTS * affected_count)
    money_share = 0.0
    for currency, amount in at_risk.items():
        total = total_money.get(currency, 0)
        if total > 0:
            money_share = max(money_share, amount / total)
    money_points = _MONEY_POINTS * money_share

    score = min(100, int(uncovered_points + affected_points + money_points))
    factors = []
    if affected_count:
        factors.append(f"affected:{affected_count}")
    if uncovered_count:
        factors.append(f"uncovered:{uncovered_count}")
    for currency in sorted(at_risk):
        factors.append(f"money_at_risk:{currency} {at_risk[currency]}")
    if blocked_count:
        factors.append(f"blocked_by_cycle:{blocked_count}")
    return score, factors


def predict_absence(snapshot, absent_ids, *, horizon_days=7, depth=6, now=None):
    """Predict what breaks when ``absent_ids`` are away ``horizon_days``.

    Runs slice 1's :func:`simulate_absence` and wraps the result with
    the snapshot fingerprint it was computed from, a 0–100 risk score
    with explainable factors, and a ``predicted_at`` timestamp. The
    same snapshot + absence set always yields the same prediction.
    """
    simulation = simulate_absence(snapshot, absent_ids, horizon_days=horizon_days, depth=depth)
    score, factors = _risk_score(simulation, snapshot)
    moment = now or datetime.now(UTC)
    nodes = {entity["id"]: entity for entity in snapshot.get("entities", [])}
    person_names = {pid: nodes.get(pid, {}).get("name", pid)
                    for pid in simulation["absent_person_ids"]}
    return {
        **simulation,
        "absent_person_names": person_names,
        "fingerprint": snapshot_fingerprint(snapshot),
        "risk_score": score,
        "factors": factors,
        "predicted_at": moment.isoformat() if isinstance(moment, datetime) else str(moment),
    }


def refresh_predictions(snapshot, absent_sets, *, previous=None, now=None):
    """Refresh horizon predictions only when the twin fingerprint moved.

    ``absent_sets`` is a list of absent-id lists (one prediction per
    set, e.g. one per key person/team). ``previous`` is the last result
    of this function (or None on first run). Returns ``{"refreshed",
    "fingerprint", "predictions"}`` — when the fingerprint is
    unchanged, ``refreshed`` is False and the previous predictions are
    returned untouched, so the watch never re-fires on an unchanged
    twin.
    """
    current = snapshot_fingerprint(snapshot)
    previous = previous or {}
    if previous.get("fingerprint") == current and previous.get("predictions") is not None:
        return {"refreshed": False, "fingerprint": current,
                "predictions": previous["predictions"]}
    predictions = [predict_absence(snapshot, absent, now=now) for absent in absent_sets]
    return {"refreshed": True, "fingerprint": current, "predictions": predictions}


@dataclass(frozen=True)
class HorizonRule:
    """One horizon-watch rule. Rules are data — thresholds, cooldowns
    and quiet-hour behaviour are configured, never hard-coded in the
    evaluator.
    """

    key: str
    severity: str  # "critical" | "warning"
    title: str
    # May contain {name}, {score}, {threshold} and {factors} placeholders,
    # filled at fire time.
    detail: str
    min_score: int = 0  # fire when risk_score >= min_score (0 = any score)
    factor: str | None = None  # fire only when a factor starts with this text
    kinds: frozenset[str] | None = None  # fire only for these entity kinds
    cooldown_seconds: int = 3600
    breaks_quiet_hours: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class HorizonAlert:
    rule_key: str
    severity: str
    person_id: str
    person_name: str
    person_kind: str
    risk_score: int
    factors: tuple[str, ...]
    fingerprint: str
    evaluated_at: datetime
    title: str
    detail: str


DEFAULT_HORIZON_RULES: tuple[HorizonRule, ...] = (
    HorizonRule(
        key="absence_critical",
        severity="critical",
        title="Critical absence risk",
        detail="{name} being away puts the twin at risk score {score} (limit {threshold}). Factors: {factors}.",
        min_score=80,
        cooldown_seconds=3600,
        breaks_quiet_hours=True,
    ),
    HorizonRule(
        key="absence_high",
        severity="warning",
        title="High absence risk",
        detail="{name} being away puts the twin at risk score {score} (limit {threshold}). Factors: {factors}.",
        min_score=60,
        cooldown_seconds=7200,
    ),
    HorizonRule(
        key="no_backup",
        severity="warning",
        title="No usable backup recorded",
        detail="{name} has uncovered work at risk (risk score {score}). Factors: {factors}.",
        min_score=40,
        factor="uncovered",
        kinds=frozenset({"person"}),
        cooldown_seconds=14400,
    ),
)


def fingerprint(rule_key: str, person_id: str) -> str:
    """Stable dedupe identity for one (rule, person) pair."""
    return hashlib.sha256(f"{rule_key}:{person_id}".encode("utf-8")).hexdigest()


def validate_rules(rules) -> list[str]:
    """Return a list of problems with a rule set (empty means valid)."""
    problems: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.key in seen:
            problems.append(f"duplicate rule key: {rule.key}")
        seen.add(rule.key)
        if rule.severity not in SEVERITIES:
            problems.append(f"rule {rule.key}: bad severity {rule.severity!r}")
        if not isinstance(rule.min_score, int) or isinstance(rule.min_score, bool):
            problems.append(f"rule {rule.key}: min_score must be an int")
        elif not 0 <= rule.min_score <= 100:
            problems.append(f"rule {rule.key}: min_score must be 0–100")
        if rule.cooldown_seconds <= 0:
            problems.append(f"rule {rule.key}: cooldown must be positive")
        if rule.factor is not None and not rule.factor:
            problems.append(f"rule {rule.key}: factor must not be empty")
        if rule.kinds is not None and not rule.kinds:
            problems.append(f"rule {rule.key}: kinds must not be empty")
    return problems


def is_quiet_hour(
    now: datetime,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> bool:
    """True when ``now`` falls inside the quiet window.

    Handles overnight windows (start > end, e.g. 22:00–07:00) as well as
    same-day windows. A naive datetime is read as org time
    (Asia/Karachi); an aware datetime is converted to org time first
    (unlike the Step AH watch, which reads an aware datetime's
    wall-clock time as-is — converting is correct when callers pass
    UTC timestamps, as the twin's predictions do).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    else:
        now = now.astimezone(ORG_TZ)
    current = now.timetz().replace(tzinfo=None)
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def rank_predictions(predictions):
    """Flatten predictions into one ranked item per absent person.

    Each item carries the prediction's risk score and factors, so the
    watch can fire per person. A team prediction contributes one item
    per team member (plus the team itself when it has no members).
    Deterministic order: highest score, then person id.
    """
    ranked = []
    for prediction in predictions or []:
        persons = prediction.get("absent_person_ids") or prediction.get("absent_ids") or []
        names = prediction.get("absent_person_names") or {}
        if not persons:
            continue
        for person_id in persons:
            ranked.append({
                "person_id": person_id,
                "person_name": names.get(person_id, person_id),
                "kind": "person",
                "risk_score": prediction["risk_score"],
                "factors": tuple(prediction.get("factors", [])),
            })
    ranked.sort(key=lambda item: (-item["risk_score"], item["person_id"]))
    return ranked


def _rule_matches(rule: HorizonRule, item: dict) -> bool:
    if item["risk_score"] < rule.min_score:
        return False
    if rule.kinds is not None and item["kind"] not in rule.kinds:
        return False
    if rule.factor is not None and not any(
        factor.startswith(rule.factor) for factor in item["factors"]
    ):
        return False
    return True


def _as_org_time(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=ORG_TZ)


def evaluate_horizon_watch(
    predictions,
    *,
    now: datetime,
    rules=DEFAULT_HORIZON_RULES,
    ranked=None,
    sent_at: dict[str, datetime] | None = None,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> dict:
    """Evaluate every enabled rule against horizon predictions.

    ``ranked`` may be a precomputed :func:`rank_predictions` result;
    otherwise the ranking is computed here. ``sent_at`` maps alert
    fingerprint -> when that (rule, person) pair last fired; a pair
    inside its cooldown window is reported under ``suppressed``
    instead of firing again. During quiet hours only rules with
    ``breaks_quiet_hours`` fire; the rest are reported under
    ``deferred``. Output is critical-first, then highest score, then
    person id, so it is deterministic.

    Returns ``{"alerts", "suppressed", "deferred", "evaluated_at"}``.
    Suppressed/deferred entries carry the same fields as an alert
    plus a ``reason`` (``"cooldown"`` / ``"quiet_hours"``).
    """
    problems = validate_rules(rules)
    if problems:
        raise ValueError("Invalid horizon-watch rules: " + "; ".join(problems))
    now = _as_org_time(now)
    sent_at = sent_at or {}
    quiet = is_quiet_hour(now, quiet_start, quiet_end)
    ranked = rank_predictions(predictions) if ranked is None else ranked

    fired: list[HorizonAlert] = []
    suppressed: list[dict] = []
    deferred: list[dict] = []
    for item in ranked:
        for rule in rules:
            if not rule.enabled:
                continue
            if not _rule_matches(rule, item):
                continue
            fp = fingerprint(rule.key, item["person_id"])
            factors = tuple(item["factors"])
            detail = rule.detail.format(
                name=item["person_name"],
                score=item["risk_score"],
                threshold=rule.min_score,
                factors=", ".join(factors) or "none",
            )
            entry = {
                "rule_key": rule.key,
                "severity": rule.severity,
                "person_id": item["person_id"],
                "person_name": item["person_name"],
                "person_kind": item["kind"],
                "risk_score": item["risk_score"],
                "factors": factors,
                "fingerprint": fp,
                "evaluated_at": now,
                "title": rule.title,
                "detail": detail,
            }
            last_sent = sent_at.get(fp)
            if last_sent is not None:
                sent = _as_org_time(last_sent)
                if (now - sent) < timedelta(seconds=rule.cooldown_seconds):
                    suppressed.append({**entry, "reason": "cooldown"})
                    continue
            if quiet and not rule.breaks_quiet_hours:
                deferred.append({**entry, "reason": "quiet_hours"})
                continue
            fired.append(HorizonAlert(**entry))

    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    fired.sort(
        key=lambda alert: (
            order.get(alert.severity, len(order)),
            -alert.risk_score,
            alert.person_id,
        )
    )
    return {
        "alerts": fired,
        "suppressed": suppressed,
        "deferred": deferred,
        "evaluated_at": now,
    }


def format_watch_message(alert: HorizonAlert) -> str:
    """Plain-language message for WhatsApp/email/in-app delivery."""
    return (
        f"[InfraSpine • digital twin • {alert.severity}] {alert.title}: "
        f"{alert.person_name} (risk score {alert.risk_score}) — {alert.detail}"
    )


__all__ = [
    "DEFAULT_HORIZON_RULES",
    "HorizonAlert",
    "HorizonRule",
    "evaluate_horizon_watch",
    "fingerprint",
    "fingerprint_changed",
    "format_watch_message",
    "is_quiet_hour",
    "predict_absence",
    "rank_predictions",
    "refresh_predictions",
    "snapshot_fingerprint",
    "validate_rules",
]
