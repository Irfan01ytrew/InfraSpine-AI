"""Twin health watch (Step K v2 slice 3).

Rules-as-data alerts over the aggregate health score, following the
Step AH / Step L watch pattern (``app/resilience/watch.py``,
``app/predictive_continuity/watch.py``): SHA-256 dedupe fingerprint
per (rule, tenant, twin version), cooldown windows, and quiet hours
22:00–07:00 Asia/Karachi where only rules with
``breaks_quiet_hours`` fire and the rest are reported as ``deferred``
(never silently dropped).

Pure functions over plain data — no I/O, no database, no network, no
secrets. Advisory only: an alert is a warning, never an action —
nothing here approves, dispatches, grants access, restores data,
switches a supplier, or pays. Alert detail carries score points and
counts only — no money amounts, no entity names — so alerts are safe
to show to any tenant member. Persistence lives in
:mod:`app.digital_twin.health_records` (alert rows); this module only
decides what fires.

Snapshot / assessment text is untrusted data. Names and attributes
are compared as data only; the watch never ``eval``s, ``exec``s, or
interprets text as an instruction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

ORG_TZ = ZoneInfo("Asia/Karachi")

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)

SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class HealthRule:
    """One twin-health watch rule. Rules are data — thresholds,
    cooldowns and quiet-hour behaviour are configured, never
    hard-coded in the evaluator.
    """

    key: str
    severity: str  # "critical" | "warning"
    title: str
    # May contain {health}, {band}, {drift}, {trust} and {stale}
    # placeholders, filled at fire time.
    detail: str
    max_health: int | None = None  # fire when health_score < this (None = ignore)
    trust_values: frozenset[str] | None = None  # fire when trust is one of these
    min_stale_sources: int = 0  # fire when stale_source_count >= this
    cooldown_seconds: int = 3600
    breaks_quiet_hours: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class HealthAlert:
    rule_key: str
    severity: str
    tenant_id: str
    health_score: int
    drift_score: int | None
    trust: str | None
    stale_sources: int
    fingerprint: str
    evaluated_at: datetime
    title: str
    detail: str


DEFAULT_RULES: tuple[HealthRule, ...] = (
    HealthRule(
        key="health_critical",
        severity="critical",
        title="Twin health is critical",
        detail="Twin health {health}/100 ({band}); drift {drift}, trust {trust}, "
               "{stale} stale source(s). Predictions from this twin may be stale.",
        max_health=40,
        cooldown_seconds=3600,
        breaks_quiet_hours=True,
    ),
    HealthRule(
        key="health_degraded",
        severity="warning",
        title="Twin health is degraded",
        detail="Twin health {health}/100 ({band}); drift {drift}, trust {trust}, "
               "{stale} stale source(s).",
        max_health=75,
        cooldown_seconds=7200,
    ),
    HealthRule(
        key="twin_stale",
        severity="critical",
        title="Twin trust is stale",
        detail="Twin trust is stale (drift {drift}, health {health}/100, "
               "{stale} stale source(s)). Do not act on predictions from this twin.",
        trust_values=frozenset({"stale"}),
        cooldown_seconds=3600,
        breaks_quiet_hours=True,
    ),
    HealthRule(
        key="stale_source",
        severity="warning",
        title="Twin source is stale",
        detail="Twin health {health}/100 ({band}); {stale} source(s) have not "
               "confirmed recently. Freshness is degrading.",
        min_stale_sources=1,
        cooldown_seconds=14400,
    ),
)


def watch_fingerprint(rule_key: str, tenant_id: str, fingerprint: str) -> str:
    """Stable dedupe identity for one (rule, tenant, twin version) firing."""
    return hashlib.sha256(
        f"{rule_key}:{tenant_id}:{fingerprint}".encode("utf-8")
    ).hexdigest()


def validate_health_rules(rules) -> list[str]:
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
        if rule.max_health is not None:
            if isinstance(rule.max_health, bool) or not isinstance(rule.max_health, int) \
                    or not 0 <= rule.max_health <= 100:
                problems.append(f"rule {rule.key}: max_health must be an int 0–100")
        if rule.trust_values is not None and (
                not rule.trust_values or not all(
                    isinstance(value, str) and value for value in rule.trust_values)):
            problems.append(f"rule {rule.key}: trust_values must not be empty")
        if isinstance(rule.min_stale_sources, bool) or not isinstance(rule.min_stale_sources, int) \
                or rule.min_stale_sources < 0:
            problems.append(f"rule {rule.key}: min_stale_sources must be a non-negative integer")
        if isinstance(rule.cooldown_seconds, bool) or not isinstance(rule.cooldown_seconds, int) \
                or rule.cooldown_seconds <= 0:
            problems.append(f"rule {rule.key}: cooldown must be a positive integer")
    return problems


def is_quiet_hour(
    now: datetime,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> bool:
    """True when ``now`` falls inside the quiet window.

    Handles overnight windows (start > end, e.g. 22:00–07:00) as well as
    same-day windows. A naive datetime is read as org time
    (Asia/Karachi) — the same convention as the Step AH / L watches.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    current = now.timetz().replace(tzinfo=None)
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def _as_org_time(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=ORG_TZ)


def _rule_matches(rule: HealthRule, assessment: dict) -> bool:
    if rule.max_health is not None and assessment["health_score"] >= rule.max_health:
        return False
    if rule.trust_values is not None and assessment.get("trust") not in rule.trust_values:
        return False
    if assessment.get("stale_source_count", 0) < rule.min_stale_sources:
        return False
    return True


def _validate_assessment(assessment):
    if not isinstance(assessment, dict):
        raise ValueError("assessment must be a compute_health result dict.")
    score = assessment.get("health_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("assessment 'health_score' must be an int 0–100.")
    fingerprint = assessment.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("assessment needs a twin version fingerprint for watch dedupe.")
    return assessment


def evaluate_health_watch(assessment, *, tenant_id, now: datetime,
                          rules=DEFAULT_RULES,
                          sent_at: dict[str, datetime] | None = None,
                          quiet_start: time = DEFAULT_QUIET_START,
                          quiet_end: time = DEFAULT_QUIET_END) -> dict:
    """Evaluate every enabled rule against a health assessment.

    ``sent_at`` maps alert fingerprint -> when that (rule, tenant,
    version) pair last fired; a pair inside its cooldown window is
    reported under ``suppressed`` instead of firing again. During
    quiet hours only rules with ``breaks_quiet_hours`` fire; the rest
    are reported under ``deferred``. Output is critical-first, then
    lowest health, then rule key — deterministic.

    Returns ``{"alerts", "suppressed", "deferred", "evaluated_at",
    "fingerprint"}``. Suppressed/deferred entries carry the same
    fields as an alert plus a ``reason`` (``"cooldown"`` /
    ``"quiet_hours"``). Alerts are advisory only: nothing here acts
    on them.
    """
    problems = validate_health_rules(rules)
    if problems:
        raise ValueError("Invalid health-watch rules: " + "; ".join(problems))
    assessment = _validate_assessment(assessment)
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string.")
    now = _as_org_time(now)
    sent_at = sent_at or {}
    quiet = is_quiet_hour(now, quiet_start, quiet_end)

    fired: list[HealthAlert] = []
    suppressed: list[dict] = []
    deferred: list[dict] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if not _rule_matches(rule, assessment):
            continue
        fp = watch_fingerprint(rule.key, tenant_id, assessment["fingerprint"])
        detail = rule.detail.format(
            health=assessment["health_score"],
            band=assessment["band"],
            drift=assessment.get("drift_score"),
            trust=assessment.get("trust"),
            stale=assessment.get("stale_source_count", 0),
        )
        record = {
            "rule_key": rule.key,
            "severity": rule.severity,
            "tenant_id": tenant_id,
            "health_score": assessment["health_score"],
            "drift_score": assessment.get("drift_score"),
            "trust": assessment.get("trust"),
            "stale_sources": assessment.get("stale_source_count", 0),
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
        fired.append(HealthAlert(**record))

    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    fired.sort(key=lambda alert: (order.get(alert.severity, len(order)), alert.health_score, alert.rule_key))
    return {
        "alerts": fired,
        "suppressed": suppressed,
        "deferred": deferred,
        "evaluated_at": now,
        "fingerprint": assessment["fingerprint"],
    }


def format_watch_message(alert: HealthAlert) -> str:
    """Plain-language message for in-app / WhatsApp / email delivery."""
    return (
        f"[InfraSpine • twin health • {alert.severity}] {alert.title}: {alert.detail}"
    )


__all__ = [
    "DEFAULT_QUIET_END",
    "DEFAULT_QUIET_START",
    "DEFAULT_RULES",
    "ORG_TZ",
    "HealthAlert",
    "HealthRule",
    "evaluate_health_watch",
    "format_watch_message",
    "is_quiet_hour",
    "validate_health_rules",
    "watch_fingerprint",
]
