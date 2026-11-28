"""What-if engine + drill comparison (slice 5): pure-function tests.

No database rows are created by these tests themselves; they run with
``--noconftest``. All data is synthetic (fake people, systems, amounts).
"""

import pytest

from app.digital_twin.whatif import (
    SCENARIOS,
    build_scenario,
    compare_drill,
    run_whatif,
    simulate_failure,
)


def person(pid, name, skills=()):
    return {"id": pid, "kind": "person", "name": name,
            "attributes": {"skills": list(skills)}}


def money(eid, kind, name, amount, currency="PKR", required=()):
    attributes = {"amount": amount, "currency": currency, "status": "awaiting_approval"}
    if required:
        attributes["required_skills"] = list(required)
    return {"id": eid, "kind": kind, "name": name, "attributes": attributes}


def entity(eid, kind, name, **attributes):
    return {"id": eid, "kind": kind, "name": name, "attributes": dict(attributes)}


def edge(eid, source, target, kind):
    return {"id": eid, "source_id": source, "target_id": target,
            "kind": kind, "confidence": 1.0, "evidence": []}


def absence_snapshot(*, backup_for=()):
    entities = [
        person("p-1", "Ayesha", ["approvals", "invoicing"]),
        person("p-3", "Sara", ["approvals"]),
        money("b-1", "invoice_batch", "March batch", 1500, required=["approvals"]),
        money("pay-1", "payment", "Payment 1", 500),
        entity("pr-1", "process", "Payroll run"),
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


def outage_snapshot():
    return {"entities": [
        entity("s-1", "system", "ERP", restorable_from_backup=True),
        entity("pr-1", "process", "Payroll run"),
        money("b-1", "invoice_batch", "March batch", 1500),
    ], "relationships": [
        edge("e-1", "pr-1", "s-1", "depends_on"),
        edge("e-2", "b-1", "pr-1", "depends_on"),
    ]}


def supplier_snapshot():
    return {"entities": [
        entity("sup-1", "supplier", "Paper Co"),
        entity("sup-2", "supplier", "Office Depot"),
        entity("pr-1", "process", "Print run"),
    ], "relationships": [
        edge("e-1", "sup-1", "pr-1", "supplies"),
        edge("e-2", "sup-2", "pr-1", "supplies"),
    ]}


def cyber_snapshot():
    return {"entities": [
        entity("s-1", "system", "Mail server"),
        entity("d-1", "data", "Employee records"),
        entity("pr-1", "process", "Onboarding"),
    ], "relationships": [
        edge("e-1", "d-1", "s-1", "documents"),
        edge("e-2", "pr-1", "s-1", "depends_on"),
    ]}


def site_snapshot():
    return {"entities": [
        entity("loc-1", "site", "Karachi office"),
        entity("s-1", "system", "File server"),
    ], "relationships": [
        edge("e-1", "s-1", "loc-1", "located_at"),
    ]}


def affected_by_id(result):
    return {entry["entity"]["id"]: entry for entry in result["affected"]}


def wave_ids(result):
    return [[step["entity_id"] for step in wave]
            for wave in result["recovery_plan"]["waves"]]


# --- build_scenario -----------------------------------------------------------

def test_build_scenario_uses_template_defaults():
    scenario = build_scenario("key_employee_leaves", ["p-1"])
    assert scenario["kind"] == "employee_absence"
    assert scenario["horizon_days"] == 7
    assert scenario["seed_ids"] == ["p-1"]


def test_build_scenario_explicit_horizon():
    assert build_scenario("server_down", ["s-1"], horizon_days=5)["horizon_days"] == 5


def test_build_scenario_rejects_bad_input():
    with pytest.raises(ValueError, match="Unknown scenario template"):
        build_scenario("nope", ["p-1"])
    with pytest.raises(ValueError, match="non-empty list"):
        build_scenario("server_down", [])
    with pytest.raises(ValueError, match="duplicates"):
        build_scenario("server_down", ["s-1", "s-1"])
    with pytest.raises(ValueError, match="positive integer"):
        build_scenario("server_down", ["s-1"], horizon_days=0)


# --- run_whatif: absence (delegates to slice 1) -------------------------------

def test_whatif_absence_delegates_and_normalizes():
    result = run_whatif(absence_snapshot(backup_for=[("p-3", "b-1")]),
                        build_scenario("key_employee_leaves", ["p-1"]))
    assert result["dry_run"] is True
    assert result["scenario"]["kind"] == "employee_absence"
    assert result["scenario"]["seed_names"] == ["Ayesha"]
    assert result["fingerprint"]
    affected = affected_by_id(result)
    assert set(affected) == {"p-1", "b-1", "pr-1", "pay-1"}
    assert affected["p-1"]["state"] == "failed"  # the absent seed
    assert affected["b-1"]["state"] == "degraded"  # covered by Sara
    assert affected["b-1"]["covered_by"] == [{"person_id": "p-3", "name": "Sara"}]
    assert affected["pr-1"]["state"] == "failed"  # uncovered
    assert affected["pay-1"]["state"] == "failed"
    assert result["failed"] == ["p-1", "pay-1", "pr-1"]
    assert result["degraded"] == ["b-1"]
    assert result["exposed"] == []
    assert result["money_at_risk"]["totals"] == {"PKR": 2000.0}
    assert result["rehearsal"] is None
    # recovery waves: batch + person first, then the dependents
    assert wave_ids(result) == [["b-1", "p-1"], ["pay-1", "pr-1"]]
    assert result["recovery_plan"]["uncovered"] != []


def test_whatif_absence_uncovered_batch_is_critical():
    result = run_whatif(absence_snapshot(),
                        build_scenario("key_employee_leaves", ["p-1"]))
    affected = affected_by_id(result)
    assert affected["b-1"]["state"] == "failed"
    assert affected["b-1"]["severity"] == "critical"  # failed + money
    assert affected["b-1"]["recovery_options"][0]["action"] == "manual_workaround"


def test_whatif_absence_rehearsal_is_practice_only():
    result = run_whatif(absence_snapshot(backup_for=[("p-3", "b-1")]),
                        build_scenario("key_employee_leaves", ["p-1"]), rehearse=True)
    rehearsal = result["rehearsal"]
    assert rehearsal["dry_run"] is True
    assert rehearsal["side_effects"] == []
    assert len(rehearsal["steps"]) == 4  # one step per affected entity
    assert [s["number"] for s in rehearsal["steps"]] == [1, 2, 3, 4]
    assert all(s["practice_only"] for s in rehearsal["steps"])
    assert rehearsal["steps"][0]["action"] == "activate_backup"


def test_whatif_is_deterministic():
    snapshot = outage_snapshot()
    scenario = build_scenario("server_down", ["s-1"])
    assert run_whatif(snapshot, scenario) == run_whatif(snapshot, scenario)


# --- run_whatif: failure kinds ------------------------------------------------

def test_whatif_system_outage_propagates_and_recovers_in_waves():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    assert result["failed"] == ["b-1", "pr-1", "s-1"]
    assert result["money_at_risk"]["totals"] == {"PKR": 1500.0}
    assert wave_ids(result) == [["s-1"], ["pr-1"], ["b-1"]]
    affected = affected_by_id(result)
    assert affected["s-1"]["recovery_options"][0]["action"] == "restore_from_backup"
    assert affected["s-1"]["severity"] == "high"
    assert affected["b-1"]["severity"] == "critical"


def test_whatif_supplier_all_providers_rule():
    one_down = run_whatif(supplier_snapshot(),
                          build_scenario("supplier_fails", ["sup-1"]))
    assert affected_by_id(one_down)["pr-1"]["state"] == "degraded"
    assert one_down["failed"] == ["sup-1"]
    assert one_down["degraded"] == ["pr-1"]
    assert affected_by_id(one_down)["pr-1"]["recovery_options"][0]["action"] == "alternate_supplier"
    both_down = run_whatif(supplier_snapshot(),
                           build_scenario("supplier_fails", ["sup-1", "sup-2"]))
    assert affected_by_id(both_down)["pr-1"]["state"] == "failed"


def test_whatif_cyber_exposes_data_and_quarantines_dependents():
    result = run_whatif(cyber_snapshot(), build_scenario("cyber_incident", ["s-1"]))
    affected = affected_by_id(result)
    assert affected["s-1"]["state"] == "failed"
    assert affected["d-1"]["state"] == "exposed"
    assert affected["d-1"]["severity"] == "low"
    assert affected["pr-1"]["state"] == "degraded"  # quarantined, not failed


def test_whatif_site_unavailable_fails_located_systems():
    result = run_whatif(site_snapshot(), build_scenario("site_unavailable", ["loc-1"]))
    assert result["failed"] == ["loc-1", "s-1"]


def test_whatif_compound_mixes_person_and_system_seeds():
    snapshot = absence_snapshot()
    snapshot["entities"].append(entity("s-1", "system", "ERP"))
    snapshot["relationships"].append(edge("e-9", "pr-1", "s-1", "depends_on"))
    scenario = {"kind": "compound", "seed_ids": ["p-1", "s-1"], "horizon_days": 7}
    result = run_whatif(snapshot, scenario)
    assert set(result["failed"]) == {"p-1", "b-1", "pr-1", "pay-1", "s-1"}
    assert result["scenario"]["kind"] == "compound"


def test_simulate_failure_direct():
    result = simulate_failure(outage_snapshot(), ["s-1"], kind="system_outage",
                              horizon_days=1)
    assert result["failed"] == ["b-1", "pr-1", "s-1"]


# --- run_whatif: validation ----------------------------------------------------

def test_whatif_rejects_bad_scenarios():
    snapshot = outage_snapshot()
    with pytest.raises(ValueError, match="Unknown scenario kind"):
        run_whatif(snapshot, {"kind": "nope", "seed_ids": ["s-1"], "horizon_days": 1})
    with pytest.raises(ValueError, match="Unknown seed id"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": ["ghost"], "horizon_days": 1})
    with pytest.raises(ValueError, match="cannot seed kind"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": ["pr-1"], "horizon_days": 1})
    with pytest.raises(ValueError, match="seed_ids must be a non-empty list"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": [], "horizon_days": 1})
    with pytest.raises(ValueError, match="positive integer"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": ["s-1"], "horizon_days": 0})
    with pytest.raises(ValueError, match="must be a dict"):
        run_whatif(snapshot, "server_down")


# --- compare_drill --------------------------------------------------------------

def _drill_from(result, drill_id="drill-1"):
    return {
        "drill_id": drill_id,
        "kind": result["scenario"]["kind"],
        "seed_ids": result["scenario"]["seed_ids"],
        "horizon_days": result["scenario"]["horizon_days"],
        "observed_affected": [entry["entity"]["id"] for entry in result["affected"]],
        "observed_money_at_risk": {"totals": dict(result["money_at_risk"]["totals"])},
        "observed_recovery_waves": wave_ids(result),
        "ran_at": "2026-11-28T10:00:00+00:00",
    }


def test_compare_drill_matches_when_drill_agrees():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    comparison = compare_drill(result, _drill_from(result))
    assert comparison["matches"] is True
    assert comparison["differences"] == []
    assert comparison["simulated_only"] == [] and comparison["observed_only"] == []
    assert comparison["money_difference"] == {}
    assert comparison["wave_match"] is True


def test_compare_drill_lists_affected_differences():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    drill = _drill_from(result)
    drill["observed_affected"] = ["s-1", "pr-1", "extra-entity"]
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["simulated_only"] == ["b-1"]
    assert comparison["observed_only"] == ["extra-entity"]
    assert any("b-1" in d for d in comparison["differences"])


def test_compare_drill_lists_money_difference():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    drill = _drill_from(result)
    drill["observed_money_at_risk"] = {"totals": {"PKR": 1000.0}}
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["money_difference"] == {"PKR": 500.0}
    assert any("500.0 PKR" in d for d in comparison["differences"])


def test_compare_drill_lists_wave_difference():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    drill = _drill_from(result)
    drill["observed_recovery_waves"] = [["s-1", "pr-1", "b-1"]]
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["wave_match"] is False
    assert any("waves differ" in d for d in comparison["differences"])


def test_compare_drill_validates_inputs():
    result = run_whatif(outage_snapshot(), build_scenario("server_down", ["s-1"]))
    with pytest.raises(ValueError, match="needs 'drill_id'"):
        compare_drill(result, {"observed_affected": [], "observed_money_at_risk": {"totals": {}},
                               "observed_recovery_waves": []})
    with pytest.raises(ValueError, match="must be a run_whatif result"):
        compare_drill({}, _drill_from(result))


def test_scenario_library_covers_founder_questions():
    assert set(SCENARIOS) == {"server_down", "key_employee_leaves", "supplier_fails",
                              "cyber_incident", "site_unavailable"}
    assert SCENARIOS["key_employee_leaves"]["kind"] == "employee_absence"
    assert SCENARIOS["supplier_fails"]["kind"] == "supplier_failure"
