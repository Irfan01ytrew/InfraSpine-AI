"""Horizon predictions + watch (slice 4): pure-function tests.

No database rows are created by these tests themselves; they run with
``--noconftest``. All data is synthetic (fake people, fake amounts).
"""

from datetime import UTC, datetime

import pytest

from app.digital_twin.horizon import (
    DEFAULT_HORIZON_RULES,
    HorizonRule,
    evaluate_horizon_watch,
    fingerprint,
    fingerprint_changed,
    format_watch_message,
    is_quiet_hour,
    predict_absence,
    rank_predictions,
    refresh_predictions,
    snapshot_fingerprint,
    validate_rules,
)

NOW = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)  # 15:00 PKT, outside quiet hours
NIGHT = datetime(2026, 11, 28, 18, 0, tzinfo=UTC)  # 23:00 PKT, quiet hours


def person(pid, name, skills=()):
    return {"id": pid, "kind": "person", "name": name,
            "attributes": {"skills": list(skills)}}


def money(eid, kind, name, amount, currency="PKR", required=()):
    attributes = {"amount": amount, "currency": currency, "status": "awaiting_approval"}
    if required:
        attributes["required_skills"] = list(required)
    return {"id": eid, "kind": kind, "name": name, "attributes": attributes}


def process(eid, name):
    return {"id": eid, "kind": "process", "name": name, "attributes": {}}


def edge(eid, source, target, kind):
    return {"id": eid, "source_id": source, "target_id": target,
            "kind": kind, "confidence": 1.0, "evidence": []}


def base_snapshot(*, backup_for=()):
    """Ayesha approves batch b-1 (1500 PKR, needs approvals); a process
    and a payment depend on it. Bilal is a recorded backup without the
    approvals skill; Sara has the skill."""
    entities = [
        person("p-1", "Ayesha", ["approvals", "invoicing"]),
        person("p-2", "Bilal", ["invoicing"]),
        person("p-3", "Sara", ["approvals"]),
        money("b-1", "invoice_batch", "March batch", 1500, required=["approvals"]),
        money("pay-1", "payment", "Payment 1", 500),
        process("pr-1", "Payroll run"),
    ]
    relationships = [
        edge("e-1", "p-1", "b-1", "approves"),
        edge("e-2", "pr-1", "b-1", "depends_on"),
        edge("e-3", "pay-1", "b-1", "depends_on"),
    ]
    for index, (backup, target) in enumerate(backup_for):
        relationships.append(edge(f"e-b{index}", backup, target, "backup_for"))
    return {"entities": entities, "relationships": relationships,
            "as_of": "2026-11-28T00:00:00+00:00"}


def big_snapshot():
    """One person approves 5 batches; each has a payment + a process.
    Everything is uncovered and all money is at risk -> score 100."""
    entities = [person("p-1", "Ayesha", ["approvals"])]
    relationships = []
    for index in range(1, 6):
        entities.append(money(f"b-{index}", "invoice_batch", f"Batch {index}", 100))
        entities.append(money(f"pay-{index}", "payment", f"Payment {index}", 50))
        entities.append(process(f"pr-{index}", f"Process {index}"))
        relationships.append(edge(f"e-a{index}", "p-1", f"b-{index}", "approves"))
        relationships.append(edge(f"e-p{index}", f"pay-{index}", f"b-{index}", "depends_on"))
        relationships.append(edge(f"e-r{index}", f"pr-{index}", f"b-{index}", "depends_on"))
    return {"entities": entities, "relationships": relationships}


# --- snapshot_fingerprint / fingerprint_changed -------------------------------

def test_fingerprint_uses_stored_value():
    snapshot = base_snapshot()
    snapshot["fingerprint"] = "abc123"
    assert snapshot_fingerprint(snapshot) == "abc123"


def test_fingerprint_computed_is_stable_and_content_sensitive():
    first = snapshot_fingerprint(base_snapshot())
    second = snapshot_fingerprint(base_snapshot())
    assert first == second and len(first) == 64
    changed = base_snapshot()
    changed["entities"] = changed["entities"][:-1]
    assert snapshot_fingerprint(changed) != first


def test_fingerprint_changed():
    snapshot = base_snapshot()
    assert fingerprint_changed(None, snapshot) is True
    fp = snapshot_fingerprint(snapshot)
    assert fingerprint_changed(fp, snapshot) is False
    assert fingerprint_changed(fp, big_snapshot()) is True


# --- predict_absence ----------------------------------------------------------

def test_predict_wraps_simulation_with_fingerprint_score_and_factors():
    snapshot = base_snapshot()
    prediction = predict_absence(snapshot, ["p-1"], now=NOW)
    assert prediction["fingerprint"] == snapshot_fingerprint(snapshot)
    assert prediction["horizon_days"] == 7
    assert prediction["predicted_at"] == NOW.isoformat()
    assert prediction["absent_names"] == ["Ayesha"]
    assert len(prediction["affected"]) == 3  # batch + process + payment
    assert prediction["money_at_risk"]["totals"] == {"PKR": 2000.0}
    # affected 3 -> 6 pts; uncovered 3 -> 45 pts; all money at risk -> 25 pts
    assert prediction["risk_score"] == 76
    assert "affected:3" in prediction["factors"]
    assert "uncovered:3" in prediction["factors"]
    assert "money_at_risk:PKR 2000.0" in prediction["factors"]


def test_predict_is_deterministic_for_same_inputs():
    snapshot = base_snapshot()
    first = predict_absence(snapshot, ["p-1"], now=NOW)
    second = predict_absence(snapshot, ["p-1"], now=NOW)
    assert first == second


def test_predict_with_covering_backup_lowers_score():
    snapshot = base_snapshot(backup_for=[("p-3", "b-1")])
    prediction = predict_absence(snapshot, ["p-1"], now=NOW)
    covered = {entry["entity"]["id"]: entry["covered_by"] for entry in prediction["affected"]}
    assert covered["b-1"] == [{"person_id": "p-3", "name": "Sara"}]
    # batch covered; process + payment still uncovered (2 x 15 = 30)
    # affected 3 -> 6 pts; money share 1.0 -> 25 pts
    assert prediction["risk_score"] == 61


def test_predict_person_with_nothing_at_risk_scores_zero():
    snapshot = base_snapshot()
    prediction = predict_absence(snapshot, ["p-2"], now=NOW)
    assert prediction["affected"] == []
    assert prediction["risk_score"] == 0
    assert prediction["factors"] == []


def test_predict_unknown_absent_id_raises():
    with pytest.raises(ValueError, match="Unknown absent id"):
        predict_absence(base_snapshot(), ["nope"], now=NOW)


def test_predict_records_horizon_days():
    prediction = predict_absence(base_snapshot(), ["p-1"], horizon_days=14, now=NOW)
    assert prediction["horizon_days"] == 14


# --- refresh_predictions ------------------------------------------------------

def test_refresh_first_run_and_unchanged_twin():
    snapshot = base_snapshot()
    first = refresh_predictions(snapshot, [["p-1"]], now=NOW)
    assert first["refreshed"] is True
    assert len(first["predictions"]) == 1
    second = refresh_predictions(snapshot, [["p-1"]], previous=first, now=NOW)
    assert second["refreshed"] is False
    assert second["predictions"] == first["predictions"]


def test_refresh_recomputes_when_fingerprint_moves():
    snapshot = base_snapshot()
    first = refresh_predictions(snapshot, [["p-1"]], now=NOW)
    assert first["predictions"][0]["risk_score"] == 76
    covered = base_snapshot(backup_for=[("p-3", "b-1")])
    second = refresh_predictions(covered, [["p-1"]], previous=first, now=NOW)
    assert second["refreshed"] is True
    assert second["predictions"][0]["risk_score"] == 61


# --- rank_predictions ---------------------------------------------------------

def test_rank_predictions_flattens_team_to_members_and_sorts():
    snapshot = base_snapshot()
    snapshot["entities"].append(
        {"id": "t-1", "kind": "team", "name": "Finance", "attributes": {}})
    snapshot["relationships"].append(edge("e-m1", "p-1", "t-1", "member_of"))
    snapshot["relationships"].append(edge("e-m2", "p-2", "t-1", "member_of"))
    prediction = predict_absence(snapshot, ["t-1"], now=NOW)
    ranked = rank_predictions([prediction])
    assert [item["person_id"] for item in ranked] == ["p-1", "p-2"]
    assert ranked[0]["risk_score"] == prediction["risk_score"]
    assert ranked[0]["person_name"] == "Ayesha"


# --- evaluate_horizon_watch ---------------------------------------------------

def test_watch_fires_high_and_no_backup_but_not_critical():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    result = evaluate_horizon_watch([prediction], now=NOW)
    keys = {alert.rule_key for alert in result["alerts"]}
    assert keys == {"absence_high", "no_backup"}
    assert result["suppressed"] == [] and result["deferred"] == []
    assert all(alert.severity == "warning" for alert in result["alerts"])


def test_watch_fires_critical_for_max_score():
    prediction = predict_absence(big_snapshot(), ["p-1"], now=NOW)
    assert prediction["risk_score"] == 100
    result = evaluate_horizon_watch([prediction], now=NOW)
    keys = {alert.rule_key for alert in result["alerts"]}
    assert keys == {"absence_critical", "absence_high", "no_backup"}
    assert result["alerts"][0].severity == "critical"


def test_watch_nothing_fires_for_zero_score():
    prediction = predict_absence(base_snapshot(), ["p-2"], now=NOW)
    result = evaluate_horizon_watch([prediction], now=NOW)
    assert result["alerts"] == []


def test_watch_cooldown_suppresses_repeat():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    fp = fingerprint("absence_high", "p-1")
    sent_at = {fp: NOW}
    result = evaluate_horizon_watch([prediction], now=NOW, sent_at=sent_at)
    assert {entry["rule_key"] for entry in result["suppressed"]} == {"absence_high"}
    assert all(entry["reason"] == "cooldown" for entry in result["suppressed"])
    assert {alert.rule_key for alert in result["alerts"]} == {"no_backup"}


def test_watch_cooldown_expires():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    fp = fingerprint("absence_high", "p-1")
    sent_at = {fp: NOW.replace(hour=7)}  # 3h earlier; cooldown is 2h
    result = evaluate_horizon_watch([prediction], now=NOW, sent_at=sent_at)
    assert {alert.rule_key for alert in result["alerts"]} == {"absence_high", "no_backup"}


def test_watch_quiet_hours_defers_warnings_only():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    result = evaluate_horizon_watch([prediction], now=NIGHT)
    assert result["alerts"] == []
    assert {entry["rule_key"] for entry in result["deferred"]} == {"absence_high", "no_backup"}
    assert all(entry["reason"] == "quiet_hours" for entry in result["deferred"])


def test_watch_critical_breaks_quiet_hours():
    prediction = predict_absence(big_snapshot(), ["p-1"], now=NOW)
    result = evaluate_horizon_watch([prediction], now=NIGHT)
    assert {alert.rule_key for alert in result["alerts"]} == {"absence_critical"}
    assert {entry["rule_key"] for entry in result["deferred"]} == {"absence_high", "no_backup"}


def test_watch_disabled_rule_never_fires():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    rules = [HorizonRule(key="off", severity="warning", title="Off",
                         detail="{name}", min_score=0, enabled=False)]
    result = evaluate_horizon_watch([prediction], now=NOW, rules=rules)
    assert result["alerts"] == []


def test_watch_invalid_rules_raise():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    bad = [HorizonRule(key="x", severity="warning", title="t", detail="{name}", min_score=0),
           HorizonRule(key="x", severity="warning", title="t", detail="{name}", min_score=0)]
    with pytest.raises(ValueError, match="duplicate rule key"):
        evaluate_horizon_watch([prediction], now=NOW, rules=bad)
    bad_score = [HorizonRule(key="y", severity="warning", title="t",
                             detail="{name}", min_score=101)]
    with pytest.raises(ValueError, match="min_score must be 0"):
        evaluate_horizon_watch([prediction], now=NOW, rules=bad_score)


def test_watch_deterministic_order():
    first = predict_absence(big_snapshot(), ["p-1"], now=NOW)
    second = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    result = evaluate_horizon_watch([second, first], now=NOW)
    assert [alert.person_id for alert in result["alerts"]] == ["p-1"] * 5
    assert result["alerts"][0].severity == "critical"
    assert result["alerts"][0].risk_score == 100


def test_fingerprint_stable_and_distinct():
    assert fingerprint("absence_high", "p-1") == fingerprint("absence_high", "p-1")
    assert fingerprint("absence_high", "p-1") != fingerprint("absence_high", "p-2")
    assert fingerprint("absence_high", "p-1") != fingerprint("no_backup", "p-1")


def test_format_watch_message_names_and_scores():
    prediction = predict_absence(base_snapshot(), ["p-1"], now=NOW)
    alert = evaluate_horizon_watch([prediction], now=NOW)["alerts"][0]
    message = format_watch_message(alert)
    assert "Ayesha" in message and "76" in message and "warning" in message


def test_default_rules_are_valid():
    assert validate_rules(DEFAULT_HORIZON_RULES) == []


# --- is_quiet_hour --------------------------------------------------------------

def test_is_quiet_hour_overnight_window():
    assert is_quiet_hour(datetime(2026, 11, 28, 23, 0)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 3, 0)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 10, 0)) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 22, 0)) is True  # start inclusive
    assert is_quiet_hour(datetime(2026, 11, 28, 7, 0)) is False  # end exclusive


def test_is_quiet_hour_naive_read_as_karachi():
    # 18:00 UTC == 23:00 PKT -> quiet; 05:00 UTC == 10:00 PKT -> not quiet.
    assert is_quiet_hour(datetime(2026, 11, 28, 18, 0, tzinfo=UTC)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 5, 0, tzinfo=UTC)) is False
