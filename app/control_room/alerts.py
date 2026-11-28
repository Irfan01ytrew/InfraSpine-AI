"""Step AF slice 1: pure-function alert-rule evaluator for the live control room.

The evaluator turns a metrics snapshot into the list of alerts that should be
sent right now, applying per-rule cooldowns (dedupe) and quiet hours. It is
pure stdlib: no I/O, no database, no network, no app imports. Sending
(WhatsApp via the Step F channels, email, in-app) is a later slice and lives
outside this module.

Safety rules enforced here:
- A missing (None) or invalid (negative) metric skips that rule — never a
  crash, never a false alert.
- Alerts are read-only notifications. Nothing in this module executes an
  action, approves anything, or changes a payee or an invoice.
- Alert text never contains secrets (no account numbers, tokens, or keys);
  only the metric value and the threshold are interpolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ORG_TZ = ZoneInfo("Asia/Karachi")

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)

SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class MetricsSnapshot:
    """One point-in-time reading of the signals the control room watches.

    Every field is optional: a signal the collector could not read is None
    and the rules that need it are skipped. Negative values are invalid and
    are treated the same as missing.
    """

    engine_on: bool | None = None
    last_tick_age_seconds: float | None = None
    queued_runs: int | None = None
    running_runs: int | None = None
    failed_runs_last_hour: int | None = None
    failed_payments_last_hour: int | None = None
    exceptions_count: int | None = None
    oldest_approval_age_hours: float | None = None
    stuck_invoices_count: int | None = None
    ingestion_workers_online: int | None = None
    failed_ingestions_last_hour: int | None = None
    llm_quota_percent: float | None = None


@dataclass(frozen=True)
class AlertRule:
    """One alert rule. Rules are data, not code — thresholds, cooldowns and
    quiet-hour behaviour are configured, never hard-coded in the evaluator."""

    key: str
    severity: str  # "critical" | "warning"
    title: str
    # May contain {value} and {threshold} placeholders, filled at fire time.
    detail: str
    threshold: float
    cooldown_seconds: int = 3600
    breaks_quiet_hours: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class Alert:
    rule_key: str
    severity: str
    title: str
    detail: str
    evaluated_at: datetime


def _valid_number(value: float | int | None) -> float | None:
    """Return the value as a float, or None when missing/invalid.

    Booleans are not numbers here (engine_on is handled separately), and
    negative readings are invalid sensor data, never a real signal.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < 0:
        return None
    return number


# Each check takes a snapshot and the rule's threshold, and returns the
# offending value when the rule fires, or None when it does not fire
# (healthy, or metric missing/invalid). The threshold is the rule's own
# (an admin override included) — never a hardcoded literal — so a
# per-rule threshold override changes the firing decision, not just the
# message text. Operators are per rule and preserved exactly: counts and
# quotas fire at >= threshold, ages and tick staleness at > threshold,
# ingestion_down at <= threshold (its default threshold is 0, i.e. no
# workers online).
def _check_worker_down(s: MetricsSnapshot, threshold: float) -> float | None:
    if s.engine_on is False:
        return 1.0
    tick = _valid_number(s.last_tick_age_seconds)
    if tick is not None and tick > threshold:
        return tick
    return None


def _check_worker_stalled(s: MetricsSnapshot, threshold: float) -> float | None:
    queued = _valid_number(s.queued_runs)
    tick = _valid_number(s.last_tick_age_seconds)
    if queued is not None and queued > 0 and tick is not None and tick > threshold:
        return tick
    return None


def _check_failed_runs_spike(s: MetricsSnapshot, threshold: float) -> float | None:
    failed = _valid_number(s.failed_runs_last_hour)
    if failed is not None and failed >= threshold:
        return failed
    return None


def _check_failed_runs_critical(s: MetricsSnapshot, threshold: float) -> float | None:
    failed = _valid_number(s.failed_runs_last_hour)
    if failed is not None and failed >= threshold:
        return failed
    return None


def _check_failed_payment(s: MetricsSnapshot, threshold: float) -> float | None:
    failed = _valid_number(s.failed_payments_last_hour)
    if failed is not None and failed >= threshold:
        return failed
    return None


def _check_exceptions_backlog(s: MetricsSnapshot, threshold: float) -> float | None:
    count = _valid_number(s.exceptions_count)
    if count is not None and count >= threshold:
        return count
    return None


def _check_exceptions_critical(s: MetricsSnapshot, threshold: float) -> float | None:
    count = _valid_number(s.exceptions_count)
    if count is not None and count >= threshold:
        return count
    return None


def _check_approval_stuck(s: MetricsSnapshot, threshold: float) -> float | None:
    age = _valid_number(s.oldest_approval_age_hours)
    if age is not None and age > threshold:
        return age
    return None


def _check_approval_stuck_critical(s: MetricsSnapshot, threshold: float) -> float | None:
    age = _valid_number(s.oldest_approval_age_hours)
    if age is not None and age > threshold:
        return age
    return None


def _check_ingestion_down(s: MetricsSnapshot, threshold: float) -> float | None:
    workers = _valid_number(s.ingestion_workers_online)
    if workers is not None and workers <= threshold:
        return workers
    return None


def _check_ingestion_failures(s: MetricsSnapshot, threshold: float) -> float | None:
    failed = _valid_number(s.failed_ingestions_last_hour)
    if failed is not None and failed >= threshold:
        return failed
    return None


def _check_llm_quota(s: MetricsSnapshot, threshold: float) -> float | None:
    quota = _valid_number(s.llm_quota_percent)
    if quota is not None and quota >= threshold:
        return quota
    return None


def _check_llm_quota_critical(s: MetricsSnapshot, threshold: float) -> float | None:
    quota = _valid_number(s.llm_quota_percent)
    if quota is not None and quota >= threshold:
        return quota
    return None


CHECKS = {
    "worker_down": _check_worker_down,
    "worker_stalled": _check_worker_stalled,
    "failed_runs_spike": _check_failed_runs_spike,
    "failed_runs_critical": _check_failed_runs_critical,
    "failed_payment": _check_failed_payment,
    "exceptions_backlog": _check_exceptions_backlog,
    "exceptions_critical": _check_exceptions_critical,
    "approval_stuck": _check_approval_stuck,
    "approval_stuck_critical": _check_approval_stuck_critical,
    "ingestion_down": _check_ingestion_down,
    "ingestion_failures": _check_ingestion_failures,
    "llm_quota": _check_llm_quota,
    "llm_quota_critical": _check_llm_quota_critical,
}


def _fmt(value: float) -> str:
    """Short human rendering: whole numbers without a decimal point."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        key="worker_down",
        severity="critical",
        title="Workflow worker down",
        detail="No engine tick for {value} s (limit {threshold} s). Runs are not moving.",
        threshold=300,
        cooldown_seconds=1800,
        breaks_quiet_hours=True,
    ),
    AlertRule(
        key="worker_stalled",
        severity="warning",
        title="Workflow worker stalled",
        detail="{value} s since the last engine tick with runs still queued (limit {threshold} s).",
        threshold=120,
        cooldown_seconds=1800,
    ),
    AlertRule(
        key="failed_runs_spike",
        severity="warning",
        title="Failed runs spiking",
        detail="{value} runs failed in the last hour (limit {threshold}).",
        threshold=5,
        cooldown_seconds=3600,
    ),
    AlertRule(
        key="failed_runs_critical",
        severity="critical",
        title="Many runs failing",
        detail="{value} runs failed in the last hour (limit {threshold}).",
        threshold=20,
        cooldown_seconds=3600,
    ),
    AlertRule(
        key="failed_payment",
        severity="critical",
        title="Payment failed",
        detail="{value} payment(s) failed in the last hour. Check before retrying.",
        threshold=1,
        cooldown_seconds=1800,
        breaks_quiet_hours=True,
    ),
    AlertRule(
        key="exceptions_backlog",
        severity="warning",
        title="Finance exceptions piling up",
        detail="{value} invoices in exception status (limit {threshold}).",
        threshold=10,
        cooldown_seconds=7200,
    ),
    AlertRule(
        key="exceptions_critical",
        severity="critical",
        title="Finance exceptions critical",
        detail="{value} invoices in exception status (limit {threshold}).",
        threshold=50,
        cooldown_seconds=7200,
    ),
    AlertRule(
        key="approval_stuck",
        severity="warning",
        title="Payment approval stuck",
        detail="Oldest unapproved payment is {value} h old (limit {threshold} h).",
        threshold=24,
        cooldown_seconds=14400,
    ),
    AlertRule(
        key="approval_stuck_critical",
        severity="critical",
        title="Payment approval stuck for days",
        detail="Oldest unapproved payment is {value} h old (limit {threshold} h).",
        threshold=72,
        cooldown_seconds=14400,
    ),
    AlertRule(
        key="ingestion_down",
        severity="warning",
        title="Document ingestion down",
        detail="No ingestion workers online.",
        threshold=0,
        cooldown_seconds=1800,
    ),
    AlertRule(
        key="ingestion_failures",
        severity="warning",
        title="Document ingestion failing",
        detail="{value} ingestions failed in the last hour (limit {threshold}).",
        threshold=5,
        cooldown_seconds=3600,
    ),
    AlertRule(
        key="llm_quota",
        severity="warning",
        title="LLM quota nearly used up",
        detail="LLM quota is at {value}% (limit {threshold}%).",
        threshold=80,
        cooldown_seconds=14400,
    ),
    AlertRule(
        key="llm_quota_critical",
        severity="critical",
        title="LLM quota almost exhausted",
        detail="LLM quota is at {value}% (limit {threshold}%).",
        threshold=95,
        cooldown_seconds=14400,
    ),
)


def validate_rules(rules) -> list[str]:
    """Return a list of problems with a rule set (empty means valid)."""
    problems: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.key in seen:
            problems.append(f"duplicate rule key: {rule.key}")
        seen.add(rule.key)
        if rule.key not in CHECKS:
            problems.append(f"unknown rule key (no check function): {rule.key}")
        if rule.severity not in SEVERITIES:
            problems.append(f"rule {rule.key}: bad severity {rule.severity!r}")
        if rule.cooldown_seconds <= 0:
            problems.append(f"rule {rule.key}: cooldown must be positive")
        if rule.threshold < 0:
            problems.append(f"rule {rule.key}: threshold must not be negative")
    return problems


def is_quiet_hour(
    now: datetime,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
) -> bool:
    """True when `now` falls inside the quiet window.

    Handles overnight windows (start > end, e.g. 22:00–07:00) as well as
    same-day windows. A naive datetime is read as org time (Asia/Karachi);
    an aware datetime is converted to org time first — quiet hours are
    org-local wall-clock hours, so a UTC instant (what the dispatcher
    passes) must not be read as if it were already org time.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    else:
        now = now.astimezone(ORG_TZ)
    current = now.timetz().replace(tzinfo=None)
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def evaluate_alerts(
    snapshot: MetricsSnapshot,
    *,
    now: datetime,
    rules=DEFAULT_RULES,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
    sent_at: dict[str, datetime] | None = None,
) -> list[Alert]:
    """Evaluate every enabled rule against the snapshot.

    `sent_at` maps rule key -> when that rule last fired; a rule inside its
    cooldown window does not fire again (dedupe). During quiet hours only
    rules with `breaks_quiet_hours` fire. Output is critical-first, stable
    rule order inside each severity. A naive `now` is read as org time.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ORG_TZ)
    sent_at = sent_at or {}
    quiet = is_quiet_hour(now, quiet_start, quiet_end)
    fired: list[Alert] = []
    for rule in rules:
        if not rule.enabled:
            continue
        check = CHECKS.get(rule.key)
        if check is None:
            continue
        value = check(snapshot, rule.threshold)
        if value is None:
            continue
        last_sent = sent_at.get(rule.key)
        if last_sent is not None:
            sent = last_sent if last_sent.tzinfo is not None else last_sent.replace(tzinfo=ORG_TZ)
            if (now - sent) < timedelta(seconds=rule.cooldown_seconds):
                continue
        if quiet and not rule.breaks_quiet_hours:
            continue
        fired.append(
            Alert(
                rule_key=rule.key,
                severity=rule.severity,
                title=rule.title,
                detail=rule.detail.format(value=_fmt(value), threshold=_fmt(float(rule.threshold))),
                evaluated_at=now,
            )
        )
    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    fired.sort(key=lambda alert: order.get(alert.severity, len(order)))
    return fired


def format_alert_message(alert: Alert) -> str:
    """Plain-language message for WhatsApp/email/in-app delivery."""
    return f"[InfraSpine • {alert.severity}] {alert.title} — {alert.detail}"


def alert_summary(alerts) -> dict[str, int]:
    """Dashboard roll-up: counts by severity plus the total."""
    summary = {"critical": 0, "warning": 0, "total": 0}
    for alert in alerts:
        if alert.severity in summary:
            summary[alert.severity] += 1
        summary["total"] += 1
    return summary
