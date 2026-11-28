"""Step AF slice 1: tests for the pure-function alert-rule evaluator.

No database, no network, no app imports beyond the stdlib-only module
under test. Run with a minimal pytest-only venv, log capture on.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.control_room.alerts import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DEFAULT_RULES,
    AlertRule,
    MetricsSnapshot,
    alert_summary,
    evaluate_alerts,
    format_alert_message,
    is_quiet_hour,
    validate_rules,
)

PKT = ZoneInfo("Asia/Karachi")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 28, hour, minute, tzinfo=PKT)


def keys(alerts):
    return [a.rule_key for a in alerts]


def healthy(**overrides):
    base = dict(
        engine_on=True,
        last_tick_age_seconds=10,
        queued_runs=0,
        running_runs=2,
        failed_runs_last_hour=0,
        failed_payments_last_hour=0,
        exceptions_count=2,
        oldest_approval_age_hours=3,
        stuck_invoices_count=0,
        ingestion_workers_online=2,
        failed_ingestions_last_hour=0,
        llm_quota_percent=40,
    )
    base.update(overrides)
    return MetricsSnapshot(**base)


# --- worker_down / worker_stalled -------------------------------------------

def test_worker_down_fires_when_engine_off():
    alerts = evaluate_alerts(healthy(engine_on=False), now=at(12))
    assert "worker_down" in keys(alerts)
    assert alerts[0].severity == "critical"


def test_worker_down_fires_on_stale_tick():
    alerts = evaluate_alerts(healthy(last_tick_age_seconds=301), now=at(12))
    assert "worker_down" in keys(alerts)


def test_worker_down_does_not_fire_just_under_threshold():
    alerts = evaluate_alerts(healthy(last_tick_age_seconds=300), now=at(12))
    assert "worker_down" not in keys(alerts)


def test_worker_stalled_needs_queued_runs_and_stale_tick():
    assert "worker_stalled" in keys(
        evaluate_alerts(healthy(queued_runs=3, last_tick_age_seconds=121), now=at(12))
    )
    assert "worker_stalled" not in keys(
        evaluate_alerts(healthy(queued_runs=0, last_tick_age_seconds=500), now=at(12))
    )
    assert "worker_stalled" not in keys(
        evaluate_alerts(healthy(queued_runs=3, last_tick_age_seconds=60), now=at(12))
    )


# --- money / finance rules ---------------------------------------------------

def test_failed_payment_fires_at_one_and_is_critical():
    alerts = evaluate_alerts(healthy(failed_payments_last_hour=1), now=at(12))
    assert "failed_payment" in keys(alerts)
    failed = [a for a in alerts if a.rule_key == "failed_payment"][0]
    assert failed.severity == "critical"


def test_exceptions_backlog_thresholds():
    assert "exceptions_backlog" in keys(
        evaluate_alerts(healthy(exceptions_count=10), now=at(12))
    )
    assert "exceptions_backlog" not in keys(
        evaluate_alerts(healthy(exceptions_count=9), now=at(12))
    )
    critical = evaluate_alerts(healthy(exceptions_count=50), now=at(12))
    assert "exceptions_critical" in keys(critical)


def test_approval_stuck_thresholds():
    assert "approval_stuck" in keys(
        evaluate_alerts(healthy(oldest_approval_age_hours=25), now=at(12))
    )
    assert "approval_stuck" not in keys(
        evaluate_alerts(healthy(oldest_approval_age_hours=24), now=at(12))
    )
    critical = evaluate_alerts(healthy(oldest_approval_age_hours=73), now=at(12))
    assert "approval_stuck_critical" in keys(critical)


# --- runs / ingestion / quota -------------------------------------------------

def test_failed_runs_spike_and_critical():
    assert "failed_runs_spike" in keys(
        evaluate_alerts(healthy(failed_runs_last_hour=5), now=at(12))
    )
    assert "failed_runs_spike" not in keys(
        evaluate_alerts(healthy(failed_runs_last_hour=4), now=at(12))
    )
    critical = evaluate_alerts(healthy(failed_runs_last_hour=20), now=at(12))
    assert "failed_runs_critical" in keys(critical)


def test_ingestion_down_fires_at_zero_workers():
    alerts = evaluate_alerts(healthy(ingestion_workers_online=0), now=at(12))
    assert "ingestion_down" in keys(alerts)


def test_ingestion_failures_threshold():
    assert "ingestion_failures" in keys(
        evaluate_alerts(healthy(failed_ingestions_last_hour=5), now=at(12))
    )
    assert "ingestion_failures" not in keys(
        evaluate_alerts(healthy(failed_ingestions_last_hour=4), now=at(12))
    )


def test_llm_quota_thresholds():
    assert "llm_quota" in keys(evaluate_alerts(healthy(llm_quota_percent=80), now=at(12)))
    assert "llm_quota" not in keys(evaluate_alerts(healthy(llm_quota_percent=79), now=at(12)))
    critical = evaluate_alerts(healthy(llm_quota_percent=95), now=at(12))
    assert "llm_quota_critical" in keys(critical)


# --- healthy snapshot fires nothing -------------------------------------------

def test_healthy_snapshot_fires_nothing():
    assert evaluate_alerts(healthy(), now=at(12)) == []


def test_empty_snapshot_fires_nothing():
    assert evaluate_alerts(MetricsSnapshot(), now=at(12)) == []


# --- missing / invalid metrics never crash or alert ---------------------------

def test_missing_metric_skips_its_rule():
    snapshot = MetricsSnapshot(engine_on=None, last_tick_age_seconds=None)
    assert evaluate_alerts(snapshot, now=at(12)) == []


@pytest.mark.parametrize("bad", [-1, -0.5, -100])
def test_negative_metric_skips_its_rule(bad):
    snapshot = MetricsSnapshot(exceptions_count=bad, failed_payments_last_hour=bad)
    assert evaluate_alerts(snapshot, now=at(12)) == []


def test_negative_tick_age_does_not_fire_worker_down():
    assert "worker_down" not in keys(
        evaluate_alerts(healthy(last_tick_age_seconds=-5), now=at(12))
    )


# --- cooldown / dedupe ----------------------------------------------------------

def test_cooldown_suppresses_repeat_inside_window():
    now = at(12)
    sent_at = {"failed_payment": now - timedelta(minutes=10)}
    alerts = evaluate_alerts(healthy(failed_payments_last_hour=2), now=now, sent_at=sent_at)
    assert "failed_payment" not in keys(alerts)


def test_cooldown_allows_refire_after_window():
    now = at(12)
    sent_at = {"failed_payment": now - timedelta(minutes=31)}
    alerts = evaluate_alerts(healthy(failed_payments_last_hour=2), now=now, sent_at=sent_at)
    assert "failed_payment" in keys(alerts)


def test_cooldown_is_per_rule():
    now = at(12)
    sent_at = {"failed_payment": now - timedelta(minutes=5)}
    alerts = evaluate_alerts(
        healthy(failed_payments_last_hour=1, exceptions_count=12), now=now, sent_at=sent_at
    )
    assert "failed_payment" not in keys(alerts)
    assert "exceptions_backlog" in keys(alerts)


# --- quiet hours ------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,expected", [(23, 30, True), (6, 59, True), (7, 0, False), (12, 0, False), (21, 59, False)])
def test_quiet_hour_window(hour, minute, expected):
    assert is_quiet_hour(at(hour, minute), DEFAULT_QUIET_START, DEFAULT_QUIET_END) is expected


def test_quiet_hours_suppress_warning():
    alerts = evaluate_alerts(healthy(exceptions_count=12), now=at(23, 30))
    assert "exceptions_backlog" not in keys(alerts)


def test_quiet_hours_allow_breaking_critical():
    alerts = evaluate_alerts(healthy(failed_payments_last_hour=1), now=at(23, 30))
    assert "failed_payment" in keys(alerts)


def test_quiet_hours_suppress_non_breaking_critical():
    alerts = evaluate_alerts(healthy(failed_runs_last_hour=25), now=at(23, 30))
    assert "failed_runs_critical" not in keys(alerts)
    assert "failed_runs_spike" not in keys(alerts)


def test_same_day_quiet_window():
    start, end = time(13, 0), time(14, 0)
    assert is_quiet_hour(at(13, 30), start, end) is True
    assert is_quiet_hour(at(14, 0), start, end) is False
    assert is_quiet_hour(at(12, 59), start, end) is False


def test_naive_datetime_is_read_as_org_time():
    naive = datetime(2026, 11, 28, 23, 30)
    assert is_quiet_hour(naive, DEFAULT_QUIET_START, DEFAULT_QUIET_END) is True


def test_utc_datetime_is_converted_to_org_time():
    """Regression: the dispatcher passes UTC instants. 18:30 UTC is 23:30
    PKT (inside quiet hours); 02:00 UTC is 07:00 PKT (outside); 07:00 UTC
    is 12:00 PKT (outside). Before the fix the UTC wall clock was read as
    if it were already org time, so warnings fired at 23:30 PKT."""
    utc = ZoneInfo("UTC")
    assert is_quiet_hour(datetime(2026, 11, 28, 18, 30, tzinfo=utc), DEFAULT_QUIET_START, DEFAULT_QUIET_END) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 2, 0, tzinfo=utc), DEFAULT_QUIET_START, DEFAULT_QUIET_END) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 7, 0, tzinfo=utc), DEFAULT_QUIET_START, DEFAULT_QUIET_END) is False


def test_evaluate_alerts_suppresses_warning_for_utc_night():
    utc = ZoneInfo("UTC")
    alerts = evaluate_alerts(
        healthy(exceptions_count=12), now=datetime(2026, 11, 28, 18, 30, tzinfo=utc)
    )
    assert "exceptions_backlog" not in keys(alerts)
    alerts = evaluate_alerts(
        healthy(failed_payments_last_hour=1), now=datetime(2026, 11, 28, 18, 30, tzinfo=utc)
    )
    assert "failed_payment" in keys(alerts)


def test_rule_threshold_override_changes_the_firing_decision():
    """Regression: the check functions used to hardcode the default
    thresholds, so a per-rule override changed only the message text,
    never whether the rule fired. 6 failed runs fire at the default 5
    but not at a raised 10 — and fire at 4 when the threshold is 3."""
    from dataclasses import replace

    raised = tuple(
        replace(rule, threshold=10) if rule.key == "failed_runs_spike" else rule
        for rule in DEFAULT_RULES
    )
    alerts = evaluate_alerts(healthy(failed_runs_last_hour=6), now=at(12), rules=raised)
    assert "failed_runs_spike" not in keys(alerts)
    alerts = evaluate_alerts(healthy(failed_runs_last_hour=6), now=at(12))
    assert "failed_runs_spike" in keys(alerts)
    lowered = tuple(
        replace(rule, threshold=3) if rule.key == "failed_runs_spike" else rule
        for rule in DEFAULT_RULES
    )
    alerts = evaluate_alerts(healthy(failed_runs_last_hour=4), now=at(12), rules=lowered)
    assert "failed_runs_spike" in keys(alerts)


# --- ordering / formatting / summary ---------------------------------------------

def test_output_is_critical_first():
    alerts = evaluate_alerts(
        healthy(exceptions_count=12, failed_payments_last_hour=1), now=at(12)
    )
    assert alerts[0].severity == "critical"
    assert alerts[-1].severity == "warning"


def test_format_alert_message_contains_title_and_severity():
    alerts = evaluate_alerts(healthy(failed_payments_last_hour=2), now=at(12))
    message = format_alert_message(alerts[0])
    assert "Payment failed" in message
    assert "critical" in message
    assert "2 payment(s) failed" in message


def test_alert_summary_counts():
    alerts = evaluate_alerts(
        healthy(failed_payments_last_hour=1, exceptions_count=12), now=at(12)
    )
    assert alert_summary(alerts) == {"critical": 1, "warning": 1, "total": 2}
    assert alert_summary([]) == {"critical": 0, "warning": 0, "total": 0}


def test_disabled_rule_never_fires():
    rules = [AlertRule(**{**r.__dict__, "enabled": False}) for r in DEFAULT_RULES]
    alerts = evaluate_alerts(healthy(engine_on=False), now=at(12), rules=rules)
    assert alerts == []


# --- rule validation --------------------------------------------------------------

def test_validate_default_rules_is_clean():
    assert validate_rules(DEFAULT_RULES) == []


def test_validate_rules_rejects_problems():
    bad = [
        AlertRule(key="worker_down", severity="critical", title="x", detail="x", threshold=1),
        AlertRule(key="worker_down", severity="critical", title="x", detail="x", threshold=1),
        AlertRule(key="no_such_rule", severity="critical", title="x", detail="x", threshold=1),
        AlertRule(key="failed_payment", severity="urgent", title="x", detail="x", threshold=1),
        AlertRule(key="llm_quota", severity="warning", title="x", detail="x", threshold=1, cooldown_seconds=0),
    ]
    problems = validate_rules(bad)
    assert any("duplicate" in p for p in problems)
    assert any("unknown" in p for p in problems)
    assert any("bad severity" in p for p in problems)
    assert any("cooldown" in p for p in problems)


def test_unknown_rule_key_is_ignored_by_evaluator():
    rules = [AlertRule(key="no_such_rule", severity="warning", title="x", detail="x", threshold=1)]
    assert evaluate_alerts(healthy(), now=at(12), rules=rules) == []
