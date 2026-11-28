"""Tests for the Step AJ continuity dashboard aggregator (slice 1).

Pure functions only: runs in a minimal pytest venv with ``--noconftest``.
Log capture stays on (no ``-p no:logging``).
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.continuity.dashboard_summary import (
    ApprovalInput,
    IncidentInput,
    RecoveryInput,
    RiskInput,
    build_dashboard_summary,
    recovery_at_risk,
    recovery_progress,
    summarize_dashboard,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def risk(**kw):
    base = {"id": "r1", "title": "Server outage", "severity": "high"}
    base.update(kw)
    return RiskInput(**base)


def incident(**kw):
    base = {"id": "i1", "title": "DB down", "severity": "high", "status": "detected"}
    base.update(kw)
    return IncidentInput(**base)


def approval(**kw):
    base = {"id": "a1", "action_type": "finance.pay", "subject": "Pay vendor"}
    base.update(kw)
    return ApprovalInput(**base)


def recovery(**kw):
    base = {"id": "c1", "title": "Restore drill", "status": "active",
            "verified_steps": 2, "total_steps": 4}
    base.update(kw)
    return RecoveryInput(**base)


# --- empty / baseline -------------------------------------------------------

def test_empty_inputs_are_ok():
    summary = build_dashboard_summary(now=NOW)
    assert summary["overall_status"] == "ok"
    assert summary["risks"]["total"] == 0
    assert summary["risks"]["by_severity"] == {}
    assert summary["incidents"]["total_active"] == 0
    assert summary["approvals"]["pending"] == 0
    assert summary["recovery"]["average_progress_percent"] is None
    assert summary["highlights"] == []
    assert summary["health"] is None
    assert summary["generated_at"] == NOW.isoformat()


def test_health_is_passed_through_unchanged():
    health = {"overall": "degraded", "components": {"api": "ok"}}
    summary = build_dashboard_summary(health=health, now=NOW)
    assert summary["health"] == health


# --- risks ------------------------------------------------------------------

def test_risk_counts_by_severity_and_top_is_worst_first():
    risks = [
        risk(id="r1", title="Low one", severity="low"),
        risk(id="r2", title="Critical one", severity="critical"),
        risk(id="r3", title="High one", severity="high"),
        risk(id="r4", title="Another critical", severity="critical"),
        risk(id="r5", title="Medium one", severity="medium"),
        risk(id="r6", title="High two", severity="high"),
    ]
    summary = build_dashboard_summary(risks=risks, now=NOW)
    assert summary["risks"]["total"] == 6
    assert summary["risks"]["by_severity"] == {
        "critical": 2, "high": 2, "medium": 1, "low": 1,
    }
    titles = [r["title"] for r in summary["risks"]["top"]]
    assert titles[:2] == ["Another critical", "Critical one"]
    assert len(summary["risks"]["top"]) == 5


def test_uncovered_critical_counts_only_critical_without_plan():
    risks = [
        risk(id="r1", severity="critical", has_plan=False),
        risk(id="r2", severity="critical", has_plan=True),
        risk(id="r3", severity="high", has_plan=False),
    ]
    summary = build_dashboard_summary(risks=risks, now=NOW)
    assert summary["risks"]["uncovered_critical"] == 1


def test_any_critical_risk_makes_overall_critical():
    summary = build_dashboard_summary(
        risks=[risk(severity="critical", has_plan=True)], now=NOW)
    assert summary["overall_status"] == "critical"


def test_noncritical_risk_makes_overall_attention():
    summary = build_dashboard_summary(risks=[risk(severity="low")], now=NOW)
    assert summary["overall_status"] == "attention"


def test_risk_output_keeps_source_affected_and_plan():
    out = build_dashboard_summary(
        risks=[risk(source="prediction", category="supplier",
                    affected=("ERP", "Vendor X"), has_plan=True)],
        now=NOW,
    )["risks"]["top"][0]
    assert out["source"] == "prediction"
    assert out["category"] == "supplier"
    assert out["affected"] == ["ERP", "Vendor X"]
    assert out["has_plan"] is True


# --- incidents --------------------------------------------------------------

def test_terminal_incidents_never_count_as_active():
    incidents = [
        incident(id="i1", status="resolved"),
        incident(id="i2", status="cancelled"),
        incident(id="i3", status="detected"),
    ]
    summary = build_dashboard_summary(incidents=incidents, now=NOW)
    assert summary["incidents"]["total_active"] == 1
    assert summary["incidents"]["by_severity"] == {"high": 1}


def test_overdue_incidents_counted_and_listed_first():
    incidents = [
        incident(id="i1", title="A minor", severity="low", overdue=False),
        incident(id="i2", title="B overdue", severity="medium", overdue=True),
    ]
    summary = build_dashboard_summary(incidents=incidents, now=NOW)
    assert summary["incidents"]["overdue"] == 1
    assert summary["incidents"]["top"][0]["title"] == "B overdue"


def test_critical_active_incident_makes_overall_critical():
    summary = build_dashboard_summary(
        incidents=[incident(severity="critical")], now=NOW)
    assert summary["overall_status"] == "critical"


def test_resolved_critical_incident_does_not_make_critical():
    summary = build_dashboard_summary(
        incidents=[incident(severity="critical", status="resolved")], now=NOW)
    assert summary["overall_status"] == "ok"


# --- approvals --------------------------------------------------------------

def test_approvals_pending_expiring_soon_and_own():
    approvals = [
        approval(id="a1", expires_at=NOW + timedelta(hours=2)),
        approval(id="a2", expires_at=NOW + timedelta(days=3)),
        approval(id="a3", expires_at=NOW - timedelta(hours=1)),
        approval(id="a4", expires_at=None, own_request=True),
    ]
    summary = build_dashboard_summary(approvals=approvals, now=NOW)
    assert summary["approvals"]["pending"] == 4
    assert summary["approvals"]["expiring_soon"] == 1
    assert summary["approvals"]["own_requests"] == 1


def test_expired_approval_is_not_expiring_soon():
    summary = build_dashboard_summary(
        approvals=[approval(expires_at=NOW - timedelta(minutes=1))], now=NOW)
    assert summary["approvals"]["expiring_soon"] == 0


def test_pending_approval_makes_overall_attention():
    summary = build_dashboard_summary(approvals=[approval()], now=NOW)
    assert summary["overall_status"] == "attention"


# --- recovery ---------------------------------------------------------------

def test_recovery_progress_math():
    assert recovery_progress(recovery(verified_steps=2, total_steps=4)) == 50.0
    assert recovery_progress(recovery(verified_steps=1, total_steps=3)) == 33.3
    assert recovery_progress(recovery(verified_steps=0, total_steps=0)) is None


def test_recovery_counts_active_closed_and_average_progress():
    recoveries = [
        recovery(id="c1", status="active", verified_steps=2, total_steps=4),
        recovery(id="c2", status="closed", verified_steps=4, total_steps=4),
        recovery(id="c3", status="pending", verified_steps=0, total_steps=0),
        recovery(id="c4", status="rejected", verified_steps=0, total_steps=2),
    ]
    summary = build_dashboard_summary(recoveries=recoveries, now=NOW)
    assert summary["recovery"]["active"] == 2  # active + pending
    assert summary["recovery"]["closed"] == 1
    # mean over the three recoveries that have steps: 50, 100, 0
    assert summary["recovery"]["average_progress_percent"] == 50.0
    items = {i["id"]: i for i in summary["recovery"]["items"]}
    assert items["c1"]["progress_percent"] == 50.0
    assert items["c3"]["progress_percent"] is None


def test_recovery_at_risk_only_when_open_and_past_rto():
    assert recovery_at_risk(
        recovery(status="active", rto_minutes=60, elapsed_minutes=61)) is True
    assert recovery_at_risk(
        recovery(status="active", rto_minutes=60, elapsed_minutes=60)) is False
    assert recovery_at_risk(
        recovery(status="closed", rto_minutes=60, elapsed_minutes=999)) is False
    assert recovery_at_risk(
        recovery(status="active", rto_minutes=None, elapsed_minutes=999)) is False
    assert recovery_at_risk(
        recovery(status="suspended", rto_minutes=30, elapsed_minutes=31)) is True


def test_recovery_past_rto_makes_overall_critical():
    summary = build_dashboard_summary(
        recoveries=[recovery(rto_minutes=60, elapsed_minutes=61)], now=NOW)
    assert summary["overall_status"] == "critical"
    assert summary["recovery"]["at_risk"] == 1


# --- highlights / summary line ----------------------------------------------

def test_highlights_worst_first_and_capped():
    summary = build_dashboard_summary(
        risks=[risk(id="r1", severity="critical", has_plan=False),
               risk(id="r2", severity="critical", has_plan=False),
               risk(id="r3", severity="critical", has_plan=False)],
        incidents=[incident(overdue=True), incident(overdue=True)],
        approvals=[approval(expires_at=NOW + timedelta(hours=1)),
                   approval()],
        recoveries=[recovery(rto_minutes=10, elapsed_minutes=11)],
        now=NOW,
    )
    assert summary["highlights"][0].startswith("3 critical risk(s)")
    assert len(summary["highlights"]) <= 6
    assert any("overdue" in h for h in summary["highlights"])
    assert any("past RTO" in h for h in summary["highlights"])


def test_summarize_dashboard_ok_attention_critical():
    ok = summarize_dashboard(build_dashboard_summary(now=NOW))
    assert ok.startswith("ok: 0 open risk(s)")
    assert "no recovery steps" in ok
    attention = summarize_dashboard(
        build_dashboard_summary(approvals=[approval()], now=NOW))
    assert attention.startswith("attention:")
    assert "1 approval(s) pending" in attention
    critical = summarize_dashboard(build_dashboard_summary(
        risks=[risk(severity="critical")],
        incidents=[incident(overdue=True)],
        recoveries=[recovery(verified_steps=1, total_steps=2)],
        now=NOW,
    ))
    assert critical.startswith("critical:")
    assert "1 critical uncovered" in critical
    assert "1 overdue" in critical
    assert "recovery 50.0% complete" in critical


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("severity", ["urgent", "CRITICAL", "", None])
def test_unknown_risk_severity_rejected(severity):
    with pytest.raises(ValueError):
        build_dashboard_summary(risks=[risk(severity=severity)], now=NOW)


@pytest.mark.parametrize("severity", ["severe", ""])
def test_unknown_incident_severity_rejected(severity):
    with pytest.raises(ValueError):
        build_dashboard_summary(incidents=[incident(severity=severity)], now=NOW)


def test_unknown_risk_source_rejected():
    with pytest.raises(ValueError):
        build_dashboard_summary(risks=[risk(source="guess")], now=NOW)


def test_unknown_recovery_status_rejected():
    with pytest.raises(ValueError):
        build_dashboard_summary(
            recoveries=[recovery(status="flying")], now=NOW)


def test_negative_steps_rejected():
    with pytest.raises(ValueError):
        recovery_progress(recovery(verified_steps=-1, total_steps=2))
    with pytest.raises(ValueError):
        recovery_progress(recovery(verified_steps=1, total_steps=-2))


def test_verified_more_than_total_rejected():
    with pytest.raises(ValueError):
        recovery_progress(recovery(verified_steps=5, total_steps=4))


def test_empty_ids_and_titles_rejected():
    with pytest.raises(ValueError):
        build_dashboard_summary(risks=[risk(id="  ")], now=NOW)
    with pytest.raises(ValueError):
        build_dashboard_summary(incidents=[incident(title="")], now=NOW)
    with pytest.raises(ValueError):
        build_dashboard_summary(approvals=[approval(action_type="")], now=NOW)
    with pytest.raises(ValueError):
        build_dashboard_summary(recoveries=[recovery(title="")], now=NOW)


def test_non_datetime_expiry_rejected():
    with pytest.raises(ValueError):
        build_dashboard_summary(
            approvals=[approval(expires_at="tomorrow")], now=NOW)
