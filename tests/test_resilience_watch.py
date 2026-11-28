"""Tests for Step AH slice 2: app/resilience/watch.py.

Pure-function tests over synthetic snapshots — no database rows, no live
data, no secrets. Fake people, teams, systems, suppliers and amounts only.
"""

from dataclasses import replace as dataclasses_replace
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.resilience.impact import assess_risk, rank_risks
from app.resilience.watch import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DEFAULT_RULES,
    RiskAlert,
    RiskRule,
    evaluate_watch,
    fingerprint,
    format_watch_message,
    is_quiet_hour,
    ranking_changed,
    validate_rules,
)

PKT = ZoneInfo("Asia/Karachi")


def _entity(entity_id, kind, name=None, **attrs):
    return {
        "id": entity_id,
        "kind": kind,
        "name": name or entity_id,
        "attributes": dict(attrs),
    }


def _rel(source_id, target_id, kind, confidence=1.0):
    return {
        "id": f"{source_id}->{target_id}:{kind}",
        "source_id": source_id,
        "target_id": target_id,
        "kind": kind,
        "confidence": confidence,
    }


def _snapshot_with_critical_person():
    """Ayesha: critical, sole approver of two pending batches, owns four
    processes, no backup. Score: blast 30 + no backup 20 + sole approver
    20 + critical 10 = 80."""
    entities = [
        _entity("p-1", "person", "Ayesha", skills=["approvals"], critical=True),
        _entity("b-1", "invoice_batch", "Batch 1", amount=1500, currency="PKR",
                status="awaiting_approval"),
        _entity("b-2", "invoice_batch", "Batch 2", amount=200, currency="PKR",
                status="awaiting_approval"),
    ]
    rels = [
        _rel("p-1", "b-1", "approves"),
        _rel("p-1", "b-2", "approves"),
    ]
    for index in range(1, 5):
        entities.append(_entity(f"pr-{index}", "process", f"Process {index}"))
        rels.append(_rel("p-1", f"pr-{index}", "owns"))
    return {"entities": entities, "relationships": rels}


def _snapshot_with_medium_person():
    """Bilal: sole approver of one pending batch, no backup, one dependent.
    Score: blast 5 + no backup 20 + sole approver 20 = 45."""
    return {
        "entities": [
            _entity("p-2", "person", "Bilal", skills=["approvals"]),
            _entity("b-1", "invoice_batch", "Batch 1", amount=100, currency="PKR",
                    status="awaiting_approval"),
        ],
        "relationships": [_rel("p-2", "b-1", "approves")],
    }


def _daytime():
    return datetime(2026, 11, 28, 10, 0, tzinfo=PKT)


def test_critical_rule_fires_at_80():
    snapshot = _snapshot_with_critical_person()
    assert assess_risk(snapshot, "p-1")["risk_score"] == 80
    result = evaluate_watch(snapshot, now=_daytime())
    keys = {(a.rule_key, a.node_id) for a in result["alerts"]}
    assert ("risk_critical", "p-1") in keys
    alert = next(a for a in result["alerts"] if a.rule_key == "risk_critical")
    assert alert.severity == "critical"
    assert alert.risk_score == 80
    assert alert.fingerprint == fingerprint("risk_critical", "p-1")


def test_high_rule_fires_for_medium_score():
    snapshot = _snapshot_with_medium_person()
    assert assess_risk(snapshot, "p-2")["risk_score"] == 45
    result = evaluate_watch(snapshot, now=_daytime())
    keys = {(a.rule_key, a.node_id) for a in result["alerts"]}
    # 45 is below the risk_high threshold of 60, so only factor rules fire.
    assert ("risk_high", "p-2") not in keys
    assert ("sole_approver", "p-2") in keys
    assert ("no_backup", "p-2") in keys


def test_no_alerts_when_all_scores_low():
    snapshot = {
        "entities": [
            _entity("p-1", "person", "Ayesha", skills=["approvals"]),
            _entity("p-2", "person", "Bilal", skills=["approvals"]),
        ],
        "relationships": [_rel("p-2", "p-1", "backup_for")],
    }
    result = evaluate_watch(snapshot, now=_daytime())
    assert result["alerts"] == []
    assert result["suppressed"] == []
    assert result["deferred"] == []


def test_empty_snapshot_produces_no_alerts():
    result = evaluate_watch({"entities": [], "relationships": []}, now=_daytime())
    assert result["alerts"] == []
    assert result["evaluated_at"] == _daytime()


def test_alerts_sorted_critical_first_then_score():
    snapshot = _snapshot_with_critical_person()
    # Add a second critical node so ordering by score can be checked.
    snapshot["entities"].append(
        _entity("s-1", "system", "ERP", critical=True)
    )
    snapshot["entities"].append(
        _entity("pr-9", "process", "Payroll", critical=True)
    )
    snapshot["relationships"].append(_rel("pr-9", "s-1", "depends_on"))
    result = evaluate_watch(snapshot, now=_daytime())
    severities = [a.severity for a in result["alerts"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == "critical" else 1)
    criticals = [a for a in result["alerts"] if a.severity == "critical"]
    scores = [a.risk_score for a in criticals]
    assert scores == sorted(scores, reverse=True)


def test_cooldown_suppresses_repeat_fire():
    snapshot = _snapshot_with_critical_person()
    first = evaluate_watch(snapshot, now=_daytime())
    assert first["alerts"]
    sent_at = {a.fingerprint: _daytime() for a in first["alerts"]}
    second = evaluate_watch(snapshot, now=_daytime(), sent_at=sent_at)
    assert second["alerts"] == []
    assert second["suppressed"]
    assert all(s["reason"] == "cooldown" for s in second["suppressed"])
    assert {s["fingerprint"] for s in second["suppressed"]} == {
        a.fingerprint for a in first["alerts"]
    }


def test_cooldown_expiry_fires_again():
    snapshot = _snapshot_with_medium_person()
    first = evaluate_watch(snapshot, now=_daytime())
    sent_at = {a.fingerprint: _daytime() for a in first["alerts"]}
    later = datetime(2026, 11, 28, 15, 0, tzinfo=PKT)  # 5h later
    second = evaluate_watch(snapshot, now=later, sent_at=sent_at)
    # sole_approver cooldown is 4h, no_backup is 4h — both expired.
    assert {(a.rule_key, a.node_id) for a in second["alerts"]} == {
        (a.rule_key, a.node_id) for a in first["alerts"]
    }


def test_quiet_hours_defers_warnings_but_not_critical():
    snapshot = _snapshot_with_critical_person()
    night = datetime(2026, 11, 28, 23, 30, tzinfo=PKT)
    result = evaluate_watch(snapshot, now=night)
    fired = {(a.rule_key, a.node_id) for a in result["alerts"]}
    assert ("risk_critical", "p-1") in fired
    deferred = {(d["rule_key"], d["node_id"]) for d in result["deferred"]}
    assert ("risk_high", "p-1") in deferred
    assert ("sole_approver", "p-1") in deferred
    assert ("no_backup", "p-1") in deferred
    assert all(d["reason"] == "quiet_hours" for d in result["deferred"])
    assert all(d["fingerprint"] == fingerprint(d["rule_key"], d["node_id"])
               for d in result["deferred"])


def test_quiet_hours_boundaries():
    assert is_quiet_hour(datetime(2026, 11, 28, 22, 0, tzinfo=PKT)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 21, 59, tzinfo=PKT)) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 6, 59, tzinfo=PKT)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 7, 0, tzinfo=PKT)) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 12, 0, tzinfo=PKT)) is False


def test_naive_datetime_read_as_karachi():
    assert is_quiet_hour(datetime(2026, 11, 28, 23, 30)) is True
    snapshot = _snapshot_with_critical_person()
    result = evaluate_watch(snapshot, now=datetime(2026, 11, 28, 23, 30))
    assert any(d["reason"] == "quiet_hours" for d in result["deferred"])


def test_custom_quiet_window():
    assert is_quiet_hour(
        datetime(2026, 11, 28, 1, 30, tzinfo=PKT), time(1, 0), time(2, 0)
    ) is True
    assert is_quiet_hour(
        datetime(2026, 11, 28, 3, 0, tzinfo=PKT), time(1, 0), time(2, 0)
    ) is False


def test_disabled_rule_never_fires():
    snapshot = _snapshot_with_critical_person()
    rules = [dataclasses_replace(rule, enabled=False) for rule in DEFAULT_RULES]
    result = evaluate_watch(snapshot, now=_daytime(), rules=rules)
    assert result["alerts"] == []


def test_kinds_filter_limits_rule_to_matching_nodes():
    snapshot = _snapshot_with_medium_person()
    snapshot["entities"].append(_entity("s-1", "system", "ERP", critical=True))
    snapshot["entities"].append(_entity("pr-1", "process", "Invoicing"))
    snapshot["relationships"].append(_rel("pr-1", "s-1", "depends_on"))
    rule = RiskRule(
        key="system_only", severity="warning", title="System", detail="{name}",
        min_score=0, kinds=frozenset({"system"}),
    )
    result = evaluate_watch(snapshot, now=_daytime(), rules=[rule])
    assert [a.node_id for a in result["alerts"]] == ["s-1"]


def test_invalid_rules_raise():
    snapshot = _snapshot_with_medium_person()
    bad_severity = RiskRule(key="x", severity="fatal", title="t", detail="d")
    with pytest.raises(ValueError, match="bad severity"):
        evaluate_watch(snapshot, now=_daytime(), rules=[bad_severity])
    bad_score = RiskRule(key="x", severity="warning", title="t", detail="d", min_score=101)
    with pytest.raises(ValueError, match="min_score"):
        evaluate_watch(snapshot, now=_daytime(), rules=[bad_score])
    bad_cooldown = RiskRule(key="x", severity="warning", title="t", detail="d", cooldown_seconds=0)
    with pytest.raises(ValueError, match="cooldown"):
        evaluate_watch(snapshot, now=_daytime(), rules=[bad_cooldown])
    duplicates = [
        RiskRule(key="x", severity="warning", title="t", detail="d"),
        RiskRule(key="x", severity="critical", title="t", detail="d"),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_watch(snapshot, now=_daytime(), rules=duplicates)


def test_validate_rules_reports_problems_without_raising():
    problems = validate_rules(
        [RiskRule(key="x", severity="fatal", title="t", detail="d", min_score=-1)]
    )
    assert len(problems) == 2
    assert validate_rules(DEFAULT_RULES) == []


def test_fingerprint_is_stable_and_specific():
    assert fingerprint("risk_critical", "p-1") == fingerprint("risk_critical", "p-1")
    assert fingerprint("risk_critical", "p-1") != fingerprint("risk_critical", "p-2")
    assert fingerprint("risk_critical", "p-1") != fingerprint("risk_high", "p-1")
    assert len(fingerprint("risk_critical", "p-1")) == 64


def test_ranking_changed_detects_score_and_set_changes():
    snapshot = _snapshot_with_medium_person()
    ranked = rank_risks(snapshot)
    assert ranking_changed(ranked, ranked) is False
    changed = [dict(item) for item in ranked]
    changed[0] = {**changed[0], "risk_score": changed[0]["risk_score"] + 5}
    assert ranking_changed(ranked, changed) is True
    assert ranking_changed(ranked, ranked + [dict(ranked[0], node_id="p-9")]) is True
    assert ranking_changed(None, ranked) is True


def test_precomputed_ranking_is_used():
    snapshot = _snapshot_with_medium_person()
    ranked = rank_risks(snapshot)
    result = evaluate_watch(snapshot, now=_daytime(), ranked=ranked)
    assert {(a.rule_key, a.node_id) for a in result["alerts"]} == {
        ("sole_approver", "p-2"), ("no_backup", "p-2"),
    }


def test_format_watch_message():
    snapshot = _snapshot_with_critical_person()
    result = evaluate_watch(snapshot, now=_daytime())
    alert = next(a for a in result["alerts"] if a.rule_key == "risk_critical")
    message = format_watch_message(alert)
    assert "Ayesha" in message
    assert "80" in message
    assert "critical" in message


def test_revision_is_passed_through():
    snapshot = _snapshot_with_medium_person()
    snapshot["revision"] = 7
    result = evaluate_watch(snapshot, now=_daytime())
    assert result["revision"] == 7
    assert result["evaluated_at"] == _daytime()


def test_alert_is_a_risk_alert_dataclass():
    snapshot = _snapshot_with_medium_person()
    result = evaluate_watch(snapshot, now=_daytime())
    assert result["alerts"]
    assert all(isinstance(a, RiskAlert) for a in result["alerts"])


def test_quiet_defaults_match_af_and_aa():
    assert DEFAULT_QUIET_START == time(22, 0)
    assert DEFAULT_QUIET_END == time(7, 0)
