"""Company Resilience AI — risk watch (slice 2).

Pure functions over a dependency snapshot and its risk ranking. No I/O, no
database, no network, no secrets. Advisory only: a watch alert is a
notification, never an action — nothing here approves, dispatches, restores
data, switches a supplier, or pays. Alert text carries node names and risk
scores only; never account numbers, tokens, or keys.

The design reuses the Step AF alert evaluator pattern
(``app/control_room/alerts.py``) and the Step AA quiet-hours rule
(``app/reports/scheduled.py``):

- Rules are data, not code: a severity threshold on the 0–100 risk score
  from :func:`app.resilience.impact.rank_risks`, optionally narrowed to a
  named risk factor (e.g. ``sole_approver``) or an entity kind.
- Dedupe: each (rule, node) pair has a stable fingerprint; a pair that
  fired inside its cooldown window does not fire again. The caller keeps
  the ``sent_at`` map (fingerprint -> last fired time) between runs.
- Quiet hours default to 22:00–07:00 Asia/Karachi. During quiet hours only
  rules with ``breaks_quiet_hours`` fire; the rest are reported as
  ``deferred`` so the caller can send them after quiet hours instead of
  silently dropping them.
- Ranking refresh: :func:`ranking_changed` tells the caller whether a new
  map revision actually moved any risk score, so the watch only
  re-evaluates when something changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.resilience.impact import rank_risks

ORG_TZ = ZoneInfo("Asia/Karachi")

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)

SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class RiskRule:
    """One risk-watch rule. Rules are data — thresholds, cooldowns and
    quiet-hour behaviour are configured, never hard-coded in the evaluator.
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
class RiskAlert:
    rule_key: str
    severity: str
    node_id: str
    node_name: str
    node_kind: str
    risk_score: int
    factors: tuple[str, ...]
    fingerprint: str
    evaluated_at: datetime
    title: str
    detail: str


DEFAULT_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        key="risk_critical",
        severity="critical",
        title="Critical resilience risk",
        detail="{name} has risk score {score} (limit {threshold}). Factors: {factors}.",
        min_score=80,
        cooldown_seconds=3600,
        breaks_quiet_hours=True,
    ),
    RiskRule(
        key="risk_high",
        severity="warning",
        title="High resilience risk",
        detail="{name} has risk score {score} (limit {threshold}). Factors: {factors}.",
        min_score=60,
        cooldown_seconds=7200,
    ),
    RiskRule(
        key="sole_approver",
        severity="warning",
        title="Approval bottleneck risk",
        detail="{name} is the sole approver of pending money (risk score {score}). Factors: {factors}.",
        min_score=0,
        factor="sole_approver",
        kinds=frozenset({"person"}),
        cooldown_seconds=14400,
    ),
    RiskRule(
        key="no_backup",
        severity="warning",
        title="No usable backup recorded",
        detail="{name} has no usable backup (risk score {score}). Factors: {factors}.",
        min_score=40,
        factor="no_backup_recorded",
        cooldown_seconds=14400,
    ),
)


def fingerprint(rule_key: str, node_id: str) -> str:
    """Stable dedupe identity for one (rule, node) pair."""
    return hashlib.sha256(f"{rule_key}:{node_id}".encode("utf-8")).hexdigest()


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
    same-day windows. A naive datetime is read as org time (Asia/Karachi).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    current = now.timetz().replace(tzinfo=None)
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def ranking_changed(old_ranked, new_ranked) -> bool:
    """True when a new ranking moved any node's risk score or the node set
    changed. The watch uses this to skip re-evaluation on an unchanged
    map revision."""
    old = {item["node_id"]: item["risk_score"] for item in (old_ranked or [])}
    new = {item["node_id"]: item["risk_score"] for item in (new_ranked or [])}
    return old != new


def _rule_matches(rule: RiskRule, ranked_node: dict) -> bool:
    if ranked_node["risk_score"] < rule.min_score:
        return False
    if rule.kinds is not None and ranked_node["kind"] not in rule.kinds:
        return False
    if rule.factor is not None and not any(
        factor.startswith(rule.factor) for factor in ranked_node["factors"]
    ):
        return False
    return True


def _as_org_time(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=ORG_TZ)


def evaluate_watch(
    snapshot,
    *,
    now: datetime,
    rules=DEFAULT_RULES,
    ranked=None,
    sent_at: dict[str, datetime] | None = None,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> dict:
    """Evaluate every enabled rule against the snapshot's risk ranking.

    ``ranked`` may be a precomputed :func:`rank_risks` result; otherwise
    the ranking is computed here. ``sent_at`` maps alert fingerprint ->
    when that (rule, node) pair last fired; a pair inside its cooldown
    window is reported under ``suppressed`` instead of firing again.
    During quiet hours only rules with ``breaks_quiet_hours`` fire; the
    rest are reported under ``deferred``. Output is critical-first, then
    highest score, then node id, so it is deterministic.

    Returns ``{"alerts", "suppressed", "deferred", "evaluated_at",
    "revision"}``. Suppressed/deferred entries carry the same fields as
    an alert plus a ``reason`` (``"cooldown"`` / ``"quiet_hours"``).
    """
    problems = validate_rules(rules)
    if problems:
        raise ValueError("Invalid risk-watch rules: " + "; ".join(problems))
    now = _as_org_time(now)
    sent_at = sent_at or {}
    quiet = is_quiet_hour(now, quiet_start, quiet_end)
    ranked = rank_risks(snapshot) if ranked is None else ranked

    fired: list[RiskAlert] = []
    suppressed: list[dict] = []
    deferred: list[dict] = []
    for node in ranked:
        for rule in rules:
            if not rule.enabled:
                continue
            if not _rule_matches(rule, node):
                continue
            fp = fingerprint(rule.key, node["node_id"])
            factors = tuple(node["factors"])
            detail = rule.detail.format(
                name=node["name"],
                score=node["risk_score"],
                threshold=rule.min_score,
                factors=", ".join(factors) or "none",
            )
            entry = {
                "rule_key": rule.key,
                "severity": rule.severity,
                "node_id": node["node_id"],
                "node_name": node["name"],
                "node_kind": node["kind"],
                "risk_score": node["risk_score"],
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
            fired.append(RiskAlert(**entry))

    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    fired.sort(
        key=lambda alert: (
            order.get(alert.severity, len(order)),
            -alert.risk_score,
            alert.node_id,
        )
    )
    return {
        "alerts": fired,
        "suppressed": suppressed,
        "deferred": deferred,
        "evaluated_at": now,
        "revision": snapshot.get("revision") if isinstance(snapshot, dict) else None,
    }


def format_watch_message(alert: RiskAlert) -> str:
    """Plain-language message for WhatsApp/email/in-app delivery."""
    return (
        f"[InfraSpine • resilience • {alert.severity}] {alert.title}: "
        f"{alert.node_name} (risk score {alert.risk_score}) — {alert.detail}"
    )
