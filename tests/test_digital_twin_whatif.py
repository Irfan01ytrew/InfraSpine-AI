"""Twin what-if dispatcher (Step K v2 slice 4): pure-function tests.

No database rows are created by these tests themselves; they run with
``--noconftest``. All data is synthetic (fake people, systems, amounts).

The real engines live on sibling branches, so the tests that need them
load the modules by file path (both are stdlib-only, so no package
import is needed): Step AL's ``app/whatif/simulator.py`` and Step S's
``app.digital_twin.impact``. Tests skip when a sibling worktree is
absent. The whole-engine comparison against Step S's ``run_whatif``
lives in ``test_digital_twin_whatif_differential.py`` (subprocesses).
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.digital_twin.drift import snapshot_fingerprint
from app.digital_twin.whatif import (
    ABSENCE_KINDS,
    FAILURE_KINDS,
    SCENARIOS,
    EngineUnavailable,
    build_scenario,
    compare_drill,
    list_templates,
    run_whatif,
    template_name,
)

ROOT = Path(__file__).resolve().parent.parent
AL_SIMULATOR = ROOT.parent / "whatif-simulations" / "app" / "whatif" / "simulator.py"
S_IMPACT = ROOT.parent / "digital-twin" / "app" / "digital_twin" / "impact.py"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def al_simulate():
    if not AL_SIMULATOR.is_file():
        pytest.skip("Step AL worktree (agent/whatif-simulations) is absent")
    return _load_module("al_simulator_for_k", str(AL_SIMULATOR)).simulate


@pytest.fixture(scope="module")
def s_simulate_absence():
    if not S_IMPACT.is_file():
        pytest.skip("Step S worktree (agent/digital-twin) is absent")
    return _load_module("s_impact_for_k", str(S_IMPACT)).simulate_absence


def _rich_snapshot():
    return {
        "entities": [
            {"id": "p-1", "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": "p-2", "kind": "person", "name": "Bilal",
             "attributes": {"skills": ["invoicing"]}},
            {"id": "t-1", "kind": "team", "name": "Finance"},
            {"id": "s-1", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True}},
            {"id": "s-2", "kind": "system", "name": "Mail",
             "attributes": {"restorable_from_backup": False}},
            {"id": "sup-1", "kind": "supplier", "name": "PaperCo"},
            {"id": "sup-2", "kind": "supplier", "name": "InkCo"},
            {"id": "proc-1", "kind": "process", "name": "Pay invoices",
             "attributes": {"required_skills": ["invoicing"]}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.0, "currency": "PKR",
                            "required_skills": ["approvals"]}},
            {"id": "d-1", "kind": "data", "name": "Ledger"},
            {"id": "site-1", "kind": "site", "name": "HQ"},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "b-1", "kind": "approves", "confidence": 1.0},
            {"id": "e-2", "source_id": "p-1", "target_id": "proc-1", "kind": "operates", "confidence": 1.0},
            {"id": "e-3", "source_id": "p-2", "target_id": "proc-1", "kind": "backup_for", "confidence": 1.0},
            {"id": "e-4", "source_id": "p-2", "target_id": "b-1", "kind": "backup_for", "confidence": 1.0},
            {"id": "e-5", "source_id": "proc-1", "target_id": "s-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "e-6", "source_id": "b-1", "target_id": "s-1", "kind": "depends_on", "confidence": 0.9},
            {"id": "e-7", "source_id": "sup-1", "target_id": "proc-1", "kind": "supplies", "confidence": 1.0},
            {"id": "e-8", "source_id": "sup-2", "target_id": "proc-1", "kind": "supplies", "confidence": 1.0},
            {"id": "e-9", "source_id": "s-1", "target_id": "d-1", "kind": "documents", "confidence": 1.0},
            {"id": "e-10", "source_id": "p-1", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
            {"id": "e-11", "source_id": "p-2", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
            {"id": "e-12", "source_id": "proc-1", "target_id": "site-1", "kind": "located_at", "confidence": 1.0},
            {"id": "e-13", "source_id": "s-2", "target_id": "s-1", "kind": "depends_on", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _cycle_snapshot():
    return {
        "entities": [
            {"id": "sys-a", "kind": "system", "name": "A",
             "attributes": {"restorable_from_backup": True}},
            {"id": "sys-b", "kind": "system", "name": "B", "attributes": {}},
            {"id": "proc-c", "kind": "process", "name": "C", "attributes": {}},
        ],
        "relationships": [
            {"id": "c-1", "source_id": "proc-c", "target_id": "sys-a", "kind": "depends_on", "confidence": 1.0},
            {"id": "c-2", "source_id": "sys-a", "target_id": "sys-b", "kind": "depends_on", "confidence": 1.0},
            {"id": "c-3", "source_id": "sys-b", "target_id": "sys-a", "kind": "depends_on", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _chain_snapshot():
    entities = [
        {"id": f"s{i}", "kind": "system", "name": f"S{i}", "attributes": {}}
        for i in range(1, 6)
    ]
    relationships = [
        {"id": f"ch-{i}", "source_id": f"s{i}", "target_id": f"s{i + 1}",
         "kind": "depends_on", "confidence": 1.0}
        for i in range(1, 5)
    ]
    return {"entities": entities, "relationships": relationships,
            "as_of": "2026-11-28T00:00:00+00:00"}


def _money_snapshot():
    return {
        "entities": [
            {"id": "sys-1", "kind": "system", "name": "ERP", "attributes": {}},
            {"id": "b-pkr", "kind": "invoice_batch", "name": "PKR batch",
             "attributes": {"amount": 1000.0, "currency": "PKR"}},
            {"id": "b-usd", "kind": "invoice_batch", "name": "USD batch",
             "attributes": {"amount": 50.0, "currency": "USD"}},
            {"id": "pay-1", "kind": "payment", "name": "Payment",
             "attributes": {"amount": 25.0, "currency": "PKR"}},
        ],
        "relationships": [
            {"id": "m-1", "source_id": "b-pkr", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "m-2", "source_id": "b-usd", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "m-3", "source_id": "pay-1", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _canned_failure_result(snapshot):
    nodes = {e["id"]: e for e in snapshot["entities"]}
    return {
        "scenario": {"kind": "system_outage", "name": "Server down", "template_id": "server_down",
                     "seed_ids": ["s-1"], "seed_names": ["ERP"], "horizon_days": 1},
        "affected": [
            {"entity": nodes["s-1"], "state": "failed", "severity": "high", "distance": 0,
             "confidence": 1.0, "path": [],
             "recovery_options": [{"action": "manual_workaround", "owner": "operations",
                                   "owner_id": None, "target": "s-1", "rank": 4,
                                   "note": "Operate manually while the failure lasts."}]},
            {"entity": nodes["proc-1"], "state": "failed", "severity": "critical", "distance": 1,
             "confidence": 1.0, "path": [],
             "recovery_options": [
                 {"action": "activate_backup", "owner": "Bilal", "owner_id": "p-2",
                  "target": "proc-1", "rank": 1, "note": "Bilal covers this."},
                 {"action": "manual_workaround", "owner": "operations", "owner_id": None,
                  "target": "proc-1", "rank": 4, "note": "Operate manually."}]},
        ],
        "failed": ["proc-1", "s-1"], "degraded": [], "exposed": [],
        "money_at_risk": {"totals": {}, "batch_count": 0, "payment_count": 0},
        "recovery_plan": {
            "waves": [[{"entity_id": "s-1", "entity_name": "ERP", "state": "failed",
                        "action": "manual_workaround", "owner": "operations", "note": "Operate manually."},
                       {"entity_id": "proc-1", "entity_name": "Pay invoices", "state": "failed",
                        "action": "activate_backup", "owner": "Bilal", "note": "Bilal covers this."}]],
            "blocked_by_dependency_cycle": [],
            "uncovered": [],
        },
        "rehearsal": None, "truncated": False, "dry_run": True,
        "assumptions": ["Simulation only."],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _canned_absence_result(snapshot):
    nodes = {e["id"]: e for e in snapshot["entities"]}
    return {
        "absent_ids": ["p-1"],
        "absent_names": ["Ayesha"],
        "absent_person_ids": ["p-1"],
        "horizon_days": 7,
        "affected": [
            {"entity": nodes["proc-1"], "distance": 1, "confidence": 1.0, "path": [],
             "covered_by": [{"person_id": "p-2", "name": "Bilal"}]},
            {"entity": nodes["b-1"], "distance": 1, "confidence": 1.0, "path": [],
             "covered_by": []},
        ],
        "uncovered": [{"entity": nodes["b-1"], "reason": "backup_missing_skills"}],
        "money_at_risk": {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0},
        "recovery_waves": [[nodes["b-1"]], [nodes["proc-1"]]],
        "blocked_by_dependency_cycle": [],
        "truncated": False,
        "assumptions": ["Simulation only."],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


class _RecordingEngine:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, snapshot, scenario, *, depth, rehearse=False):
        self.calls.append({"snapshot": snapshot, "scenario": scenario,
                           "depth": depth, "rehearse": rehearse})
        return copy.deepcopy(self.result)


class _RecordingAbsenceEngine:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, snapshot, absent_ids, *, horizon_days, depth):
        self.calls.append({"snapshot": snapshot, "absent_ids": absent_ids,
                           "horizon_days": horizon_days, "depth": depth})
        return copy.deepcopy(self.result)


def _wave_ids(result):
    return [[step["entity_id"] for step in wave]
            for wave in result["recovery_plan"]["waves"]]


# --- scenario library -----------------------------------------------------------

def test_build_scenario_templates():
    assert build_scenario("server_down", ["s-1"]) == {
        "template_id": "server_down", "kind": "system_outage", "name": "Server down",
        "description": "A system or service is unavailable (outage).",
        "seed_ids": ["s-1"], "horizon_days": 1,
    }
    assert build_scenario("key_employee_leaves", ["p-1"])["horizon_days"] == 7
    assert build_scenario("supplier_fails", ["sup-1"])["horizon_days"] == 14
    assert build_scenario("cyber_incident", ["s-1"])["horizon_days"] == 3
    assert build_scenario("site_unavailable", ["site-1"])["horizon_days"] == 7
    assert build_scenario("server_down", ["s-1"], horizon_days=5)["horizon_days"] == 5


def test_build_scenario_rejects_bad_input():
    with pytest.raises(ValueError, match="Unknown scenario template"):
        build_scenario("nope", ["s-1"])
    with pytest.raises(ValueError, match="seed_ids must be a non-empty list"):
        build_scenario("server_down", [])
    with pytest.raises(ValueError, match="must not contain duplicates"):
        build_scenario("server_down", ["s-1", "s-1"])
    with pytest.raises(ValueError, match="non-empty string"):
        build_scenario("server_down", [""])
    with pytest.raises(ValueError, match="positive integer"):
        build_scenario("server_down", ["s-1"], horizon_days=0)


def test_list_templates_matches_library():
    templates = list_templates()
    assert {t["template_id"] for t in templates} == set(SCENARIOS)
    assert len(templates) == 5
    for template in templates:
        assert template["kind"] == SCENARIOS[template["template_id"]]["kind"]
        assert template["horizon_days"] == SCENARIOS[template["template_id"]]["default_horizon_days"]
    assert template_name("server_down") == "Server down"
    assert template_name("nope") is None


# --- dispatcher routing (fake engines) ------------------------------------------

def test_failure_kind_delegates_to_engine_and_enriches():
    snapshot = _rich_snapshot()
    scenario = build_scenario("server_down", ["s-1"])
    engine = _RecordingEngine(_canned_failure_result(snapshot))
    absence = _RecordingAbsenceEngine(_canned_absence_result(snapshot))
    result = run_whatif(snapshot, scenario, engine=engine, absence_engine=absence)

    assert len(engine.calls) == 1
    assert engine.calls[0]["depth"] == 6
    assert engine.calls[0]["rehearse"] is False
    assert engine.calls[0]["scenario"] == scenario
    assert absence.calls == []  # absence engine never touched for a failure kind
    assert result["fingerprint"] == snapshot_fingerprint(snapshot)
    assert result["dry_run"] is True
    assert result["rehearsal"] is None
    assert result["scenario"]["seed_names"] == ["ERP"]
    assert result["scenario"]["template_id"] == "server_down"
    assert result["as_of"] == "2026-11-28T00:00:00+00:00"
    assert result["assumptions"]
    by_id = {e["entity"]["id"]: e for e in result["affected"]}
    assert by_id["proc-1"]["covered_by"] == [{"person_id": "p-2", "name": "Bilal"}]
    assert by_id["s-1"]["covered_by"] == []
    # The engine's result dict is not mutated by the enrichment.
    assert "covered_by" not in engine.result["affected"][0]
    assert "fingerprint" not in engine.result


def test_rehearse_builds_numbered_practice_steps():
    snapshot = _rich_snapshot()
    engine = _RecordingEngine(_canned_failure_result(snapshot))
    result = run_whatif(snapshot, build_scenario("server_down", ["s-1"]),
                        rehearse=True, engine=engine)
    rehearsal = result["rehearsal"]
    assert rehearsal["dry_run"] is True
    assert rehearsal["side_effects"] == []
    assert [s["number"] for s in rehearsal["steps"]] == [1, 2]
    assert all(s["practice_only"] is True for s in rehearsal["steps"])
    assert rehearsal["steps"][0]["entity_id"] == "s-1"
    assert rehearsal["steps"][1]["entity_id"] == "proc-1"


def test_absence_kind_uses_absence_engine_not_failure_engine():
    snapshot = _rich_snapshot()
    scenario = build_scenario("key_employee_leaves", ["p-1"])
    engine = _RecordingEngine(_canned_failure_result(snapshot))
    absence = _RecordingAbsenceEngine(_canned_absence_result(snapshot))
    result = run_whatif(snapshot, scenario, engine=engine, absence_engine=absence)

    assert engine.calls == []  # the failure engine never sees an absence kind
    assert len(absence.calls) == 1
    assert absence.calls[0]["absent_ids"] == ["p-1"]
    assert absence.calls[0]["horizon_days"] == 7
    assert result["failed"] == ["b-1", "p-1"]
    assert result["degraded"] == ["proc-1"]
    assert result["exposed"] == []
    by_id = {e["entity"]["id"]: e for e in result["affected"]}
    assert by_id["proc-1"]["state"] == "degraded"
    assert by_id["proc-1"]["covered_by"] == [{"person_id": "p-2", "name": "Bilal"}]
    assert by_id["proc-1"]["recovery_options"][0]["action"] == "activate_backup"
    assert by_id["b-1"]["state"] == "failed"
    assert by_id["p-1"]["state"] == "failed"
    assert by_id["p-1"]["distance"] == 0
    assert by_id["p-1"]["path"] == []
    assert by_id["p-1"]["recovery_options"][0]["note"] == "Cover this person's work while they are away."
    assert result["money_at_risk"] == {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0}
    assert result["fingerprint"] == snapshot_fingerprint(snapshot)
    assert result["dry_run"] is True


def test_run_whatif_rejects_bad_scenarios():
    snapshot = _rich_snapshot()
    engine = _RecordingEngine(_canned_failure_result(snapshot))
    with pytest.raises(ValueError, match="must be a dict"):
        run_whatif(snapshot, "server_down", engine=engine)
    with pytest.raises(ValueError, match="Unknown scenario kind"):
        run_whatif(snapshot, {"kind": "nope", "seed_ids": ["s-1"], "horizon_days": 1}, engine=engine)
    with pytest.raises(ValueError, match="positive integer"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": ["s-1"], "horizon_days": 0}, engine=engine)
    with pytest.raises(ValueError, match="positive integer"):
        run_whatif(snapshot, {"kind": "system_outage", "seed_ids": ["s-1"], "horizon_days": 1},
                   depth=0, engine=engine)


def test_missing_engine_raises_engine_unavailable_naming_the_module():
    snapshot = _rich_snapshot()
    # Neither Step AL nor Step S has merged on this branch, so the lazy
    # imports fail and the error must name the missing module.
    with pytest.raises(EngineUnavailable, match="app.whatif.simulator"):
        run_whatif(snapshot, build_scenario("server_down", ["s-1"]))
    with pytest.raises(EngineUnavailable, match="app.digital_twin.impact"):
        run_whatif(snapshot, build_scenario("key_employee_leaves", ["p-1"]))


def test_malformed_engine_result_is_rejected():
    snapshot = _rich_snapshot()
    scenario = build_scenario("server_down", ["s-1"])
    with pytest.raises(ValueError, match="must return a simulation result"):
        run_whatif(snapshot, scenario, engine=_RecordingEngine({"nope": True}))
    bad = _canned_failure_result(snapshot)
    del bad["scenario"]
    with pytest.raises(ValueError, match="scenario.seed_names"):
        run_whatif(snapshot, scenario, engine=_RecordingEngine(bad))


# --- real Step AL engine (injected by file path) --------------------------------

def test_real_al_engine_failure_kinds(al_simulate):
    snapshot = _rich_snapshot()
    cases = [
        ("server_down", ["s-1"], {"failed": ["b-1", "proc-1", "s-1", "s-2"]}),
        ("supplier_fails", ["sup-1"], {"failed": ["sup-1"], "degraded": ["proc-1"]}),
        ("supplier_fails", ["sup-1", "sup-2"], {"failed": ["proc-1", "sup-1", "sup-2"]}),
        ("cyber_incident", ["s-1"], {"exposed": ["d-1"]}),
        ("site_unavailable", ["site-1"], {"failed": ["proc-1", "site-1"]}),
    ]
    for template_id, seeds, expected in cases:
        result = run_whatif(snapshot, build_scenario(template_id, seeds), engine=al_simulate)
        for key, value in expected.items():
            assert result[key] == value, (template_id, seeds, key)
        assert result["dry_run"] is True
        assert result["fingerprint"] == snapshot_fingerprint(snapshot)
        assert all("covered_by" in e for e in result["affected"])
        # Deterministic: the same inputs give byte-identical results.
        again = run_whatif(snapshot, build_scenario(template_id, seeds), engine=al_simulate)
        assert json.dumps(result, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str)


def test_real_al_engine_compound_and_money(al_simulate):
    snapshot = _rich_snapshot()
    result = run_whatif(snapshot, build_scenario("server_down", ["s-1"], horizon_days=1),
                        engine=al_simulate)
    assert result["money_at_risk"] == {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0}
    compound = {"kind": "compound", "seed_ids": ["s-1", "sup-1"], "horizon_days": 7}
    result = run_whatif(snapshot, compound, engine=al_simulate)
    assert "s-1" in result["failed"] and "sup-1" in result["failed"]
    assert result["money_at_risk"]["totals"] == {"PKR": 1500.0}

    money = run_whatif(_money_snapshot(), build_scenario("server_down", ["sys-1"]),
                       engine=al_simulate)
    assert money["money_at_risk"] == {
        "totals": {"PKR": 1025.0, "USD": 50.0}, "batch_count": 2, "payment_count": 1}


def test_real_al_engine_cycle_blocks_all_waves(al_simulate):
    result = run_whatif(_cycle_snapshot(), build_scenario("server_down", ["sys-b"]),
                        engine=al_simulate)
    assert result["failed"] == ["proc-c", "sys-a", "sys-b"]
    assert result["recovery_plan"]["waves"] == []
    assert {e["id"] for e in result["recovery_plan"]["blocked_by_dependency_cycle"]} == {
        "proc-c", "sys-a", "sys-b"}


def test_real_al_engine_depth_limit_truncates(al_simulate):
    result = run_whatif(_chain_snapshot(),
                        {"kind": "system_outage", "seed_ids": ["s5"], "horizon_days": 1},
                        depth=2, engine=al_simulate)
    assert {e["entity"]["id"] for e in result["affected"]} == {"s5", "s4", "s3"}
    assert result["truncated"] is True


def test_real_al_engine_rejects_invalid_snapshots(al_simulate):
    scenario = build_scenario("server_down", ["s-1"])
    base = _rich_snapshot()
    duplicate = copy.deepcopy(base)
    duplicate["entities"].append(dict(base["entities"][0]))
    with pytest.raises(ValueError, match="Duplicate entity id"):
        run_whatif(duplicate, scenario, engine=al_simulate)
    dangling = copy.deepcopy(base)
    dangling["relationships"].append(
        {"id": "e-x", "source_id": "ghost", "target_id": "s-1", "kind": "depends_on", "confidence": 1.0})
    with pytest.raises(ValueError, match="unknown source_id"):
        run_whatif(dangling, scenario, engine=al_simulate)
    negative = copy.deepcopy(base)
    negative["entities"][8]["attributes"]["amount"] = -5
    with pytest.raises(ValueError, match="must be >= 0"):
        run_whatif(negative, scenario, engine=al_simulate)
    bad_confidence = copy.deepcopy(base)
    bad_confidence["relationships"][0]["confidence"] = 1.5
    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        run_whatif(bad_confidence, scenario, engine=al_simulate)


# --- real Step S absence engine (injected by file path) --------------------------

def test_real_s_absence_engine_employee(s_simulate_absence):
    snapshot = _rich_snapshot()
    result = run_whatif(snapshot, build_scenario("key_employee_leaves", ["p-1"]),
                        absence_engine=s_simulate_absence)
    # Bilal covers the process (has invoicing), but not the batch
    # (lacks approvals) — so the process is degraded, the batch failed.
    assert result["degraded"] == ["proc-1"]
    assert result["failed"] == ["b-1", "p-1"]
    assert result["exposed"] == []
    assert result["money_at_risk"] == {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0}
    assert result["fingerprint"] == snapshot_fingerprint(snapshot)


def test_real_s_absence_engine_team(s_simulate_absence):
    snapshot = _rich_snapshot()
    result = run_whatif(snapshot, build_scenario("key_employee_leaves", ["p-1"]),
                        absence_engine=s_simulate_absence)
    team = run_whatif(snapshot, {"kind": "team_absence", "seed_ids": ["t-1"], "horizon_days": 7},
                      absence_engine=s_simulate_absence)
    # Both members are away, so no backup is left: everything fails.
    assert team["failed"] == ["b-1", "p-1", "p-2", "proc-1"]
    assert team["degraded"] == []
    # The team node itself is never an affected entity for the twin.
    assert "t-1" not in {e["entity"]["id"] for e in team["affected"]}
    assert result["failed"] == ["b-1", "p-1"]


# --- compare_drill ---------------------------------------------------------------

def _drill_from(result, drill_id="drill-1"):
    return {
        "drill_id": drill_id,
        "kind": result["scenario"]["kind"],
        "seed_ids": result["scenario"]["seed_ids"],
        "horizon_days": result["scenario"]["horizon_days"],
        "observed_affected": [entry["entity"]["id"] for entry in result["affected"]],
        "observed_money_at_risk": {"totals": dict(result["money_at_risk"]["totals"])},
        "observed_recovery_waves": _wave_ids(result),
        "ran_at": "2026-11-28T10:00:00+00:00",
    }


def test_compare_drill_matches_when_drill_agrees(al_simulate):
    result = run_whatif(_rich_snapshot(), build_scenario("server_down", ["s-1"]),
                        engine=al_simulate)
    comparison = compare_drill(result, _drill_from(result))
    assert comparison["matches"] is True
    assert comparison["differences"] == []
    assert comparison["simulated_only"] == [] and comparison["observed_only"] == []
    assert comparison["money_difference"] == {}
    assert comparison["wave_match"] is True
    assert comparison["simulation_fingerprint"] == result["fingerprint"]


def test_compare_drill_lists_affected_differences(al_simulate):
    result = run_whatif(_rich_snapshot(), build_scenario("server_down", ["s-1"]),
                        engine=al_simulate)
    drill = _drill_from(result)
    drill["observed_affected"] = ["s-1", "proc-1", "extra-entity"]
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["simulated_only"] == ["b-1", "s-2"]
    assert comparison["observed_only"] == ["extra-entity"]
    assert any("b-1" in d for d in comparison["differences"])


def test_compare_drill_lists_money_difference(al_simulate):
    result = run_whatif(_rich_snapshot(), build_scenario("server_down", ["s-1"]),
                        engine=al_simulate)
    drill = _drill_from(result)
    drill["observed_money_at_risk"] = {"totals": {"PKR": 1000.0}}
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["money_difference"] == {"PKR": 500.0}
    assert any("500.0 PKR" in d for d in comparison["differences"])


def test_compare_drill_lists_wave_difference(al_simulate):
    result = run_whatif(_rich_snapshot(), build_scenario("server_down", ["s-1"]),
                        engine=al_simulate)
    drill = _drill_from(result)
    drill["observed_recovery_waves"] = [["s-1", "proc-1", "b-1", "s-2"]]
    comparison = compare_drill(result, drill)
    assert comparison["matches"] is False
    assert comparison["wave_match"] is False
    assert any("waves differ" in d for d in comparison["differences"])


def test_compare_drill_rejects_bad_input(al_simulate):
    result = run_whatif(_rich_snapshot(), build_scenario("server_down", ["s-1"]),
                        engine=al_simulate)
    with pytest.raises(ValueError, match="run_whatif result"):
        compare_drill({"nope": True}, _drill_from(result))
    with pytest.raises(ValueError, match="drill must be a dict"):
        compare_drill(result, "drill-1")
    drill = _drill_from(result)
    del drill["observed_affected"]
    with pytest.raises(ValueError, match="drill record needs"):
        compare_drill(result, drill)
    drill = _drill_from(result)
    drill["observed_money_at_risk"] = {"totals": "PKR"}
    with pytest.raises(ValueError, match="dict with a 'totals' dict"):
        compare_drill(result, drill)
