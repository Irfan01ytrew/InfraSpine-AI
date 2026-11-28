"""Step W slice 1 — fault detector tests (pure functions, no DB, no network).

Run: pytest tests/test_self_healing_detector.py -v --noconftest
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.self_healing.detector import (
    COOLDOWN_SECONDS,
    MAX_ATTEMPTS,
    AttemptHistory,
    Incident,
    Probe,
    classify_incident,
    decide_action,
    escalation_report,
    summarize_incidents,
)


def ok_probes():
    return [Probe(c, "ok") for c in ("api", "database", "workflow_worker", "ingestion_worker", "vector_index", "backup")]


# --- classification ---

def test_all_ok_returns_none():
    assert classify_incident(ok_probes()) is None


def test_database_down_is_critical():
    incident = classify_incident([Probe("database", "down", age_seconds=5)])
    assert incident is not None
    assert incident.kind == "database_unreachable"
    assert incident.severity == "critical"


def test_api_down_is_critical():
    incident = classify_incident([Probe("api", "down", age_seconds=5)])
    assert incident is not None
    assert incident.kind == "api_down"
    assert incident.severity == "critical"


def test_database_beats_api_when_both_down():
    incident = classify_incident([Probe("database", "down"), Probe("api", "down")])
    assert incident is not None
    assert incident.kind == "database_unreachable"


def test_stale_worker_heartbeat_is_worker_stalled():
    incident = classify_incident(ok_probes(), worker_heartbeat_age_seconds=601)
    assert incident is not None
    assert incident.kind == "worker_stalled"
    assert incident.severity == "high"


def test_heartbeat_exactly_at_threshold_is_not_stalled():
    assert classify_incident(ok_probes(), worker_heartbeat_age_seconds=300) is None


def test_error_rate_spike_above_threshold():
    incident = classify_incident(ok_probes(), error_total=200, error_count=30)
    assert incident is not None
    assert incident.kind == "error_rate_spike"
    assert "15.0%" in incident.summary


def test_error_rate_below_threshold_is_fine():
    assert classify_incident(ok_probes(), error_total=200, error_count=8) is None


def test_error_rate_with_too_few_requests_is_fine():
    assert classify_incident(ok_probes(), error_total=20, error_count=10) is None


def test_stale_lock_is_detected():
    incident = classify_incident(ok_probes(), lock_age_seconds=601)
    assert incident is not None
    assert incident.kind == "stale_lock"
    assert incident.severity == "medium"


def test_lock_exactly_at_threshold_is_not_stale():
    assert classify_incident(ok_probes(), lock_age_seconds=600) is None


def test_degraded_worker_is_ingestion_backlog():
    incident = classify_incident([Probe("ingestion_worker", "degraded", age_seconds=120)])
    assert incident is not None
    assert incident.kind == "ingestion_backlog"
    assert incident.component == "ingestion_worker"


def test_degraded_ingestion_is_not_shadowed_by_unknown_workflow_worker():
    # The collector always emits all six probes; an unknown workflow probe
    # must not hide a degraded ingestion probe (Agent 149 regression).
    incident = classify_incident([
        Probe("workflow_worker", "unknown", age_seconds=0),
        Probe("ingestion_worker", "degraded", age_seconds=120),
    ])
    assert incident is not None
    assert incident.kind == "ingestion_backlog"
    assert incident.component == "ingestion_worker"


def test_stale_unknown_probe_is_unknown_kind():
    incident = classify_incident([Probe("backup", "unknown", age_seconds=900)])
    assert incident is not None
    assert incident.kind == "unknown"
    assert incident.severity == "high"


# --- probe validation ---

def test_unknown_component_rejected():
    with pytest.raises(ValueError):
        Probe("teleporter", "ok")


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        Probe("api", "exploded")


def test_negative_age_rejected():
    with pytest.raises(ValueError):
        Probe("api", "ok", age_seconds=-1)


# --- remedy vs escalate ---

def _incident(kind, severity="low"):
    return Incident(key=kind, kind=kind, severity=severity, component="api", summary="test")


def test_transient_failure_gets_retry_once():
    decision = decide_action(_incident("transient_failure", "low"))
    assert decision.action == "remedy"
    assert decision.remedy == "retry_once"


def test_stale_lock_gets_clear_lock():
    decision = decide_action(_incident("stale_lock", "medium"))
    assert decision.action == "remedy"
    assert decision.remedy == "clear_stale_lock"


def test_critical_incident_always_escalates():
    decision = decide_action(_incident("api_down", "critical"))
    assert decision.action == "escalate"
    assert decision.remedy is None


def test_unknown_kind_escalates():
    decision = decide_action(_incident("unknown", "high"))
    assert decision.action == "escalate"


def test_attempt_budget_exhausted_escalates():
    history = AttemptHistory(attempts=MAX_ATTEMPTS)
    decision = decide_action(_incident("transient_failure", "low"), history)
    assert decision.action == "escalate"
    assert "budget" in decision.reason


def test_cooldown_blocks_repeat_remedy():
    now = datetime.now(UTC)
    history = AttemptHistory(attempts=1, last_attempt_at=now - timedelta(seconds=60))
    decision = decide_action(_incident("transient_failure", "low"), history, now=now)
    assert decision.action == "escalate"
    assert "cooldown" in decision.reason


def test_remedy_allowed_after_cooldown():
    now = datetime.now(UTC)
    history = AttemptHistory(attempts=1, last_attempt_at=now - timedelta(seconds=COOLDOWN_SECONDS + 10))
    decision = decide_action(_incident("transient_failure", "low"), history, now=now)
    assert decision.action == "remedy"


# --- escalation report ---

def test_report_scrubs_secrets():
    incident = Incident(
        key="database_unreachable",
        kind="database_unreachable",
        severity="critical",
        component="database",
        summary="DB down",
        evidence="conn failed: postgresql://user:hunter2-secret@127.0.0.1/db token=abc123",
    )
    report = escalation_report(incident, AttemptHistory(attempts=2))
    assert "hunter2" not in report["evidence"]
    assert "abc123" not in report["evidence"]
    assert "[redacted]" in report["evidence"]
    assert report["needs_owner"] is True
    assert report["auto_remedy_attempts"] == 2
    assert "did not change any data" in report["note"]


# --- summary ---

def test_summarize_counts_and_worst():
    incidents = [
        _incident("transient_failure", "low"),
        _incident("stale_lock", "medium"),
        _incident("worker_stalled", "high"),
    ]
    summary = summarize_incidents(incidents)
    assert summary["total"] == 3
    assert summary["by_severity"] == {"low": 1, "medium": 1, "high": 1, "critical": 0}
    assert summary["worst"] == "high"


def test_summarize_empty():
    assert summarize_incidents([]) == {
        "total": 0,
        "by_severity": {"low": 0, "medium": 0, "high": 0, "critical": 0},
        "worst": None,
    }
