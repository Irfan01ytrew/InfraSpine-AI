"""Tests for the drill comparison (Step AL slice 4).

Pure-function tests: no database, no network. Run with
``--noconftest`` in a minimal venv because the repo conftest needs
the full app stack and this module is pure stdlib.
"""

import copy

import pytest

from app.whatif import drills
from app.whatif.simulator import build_scenario, simulate

ERP = "11111111-1111-1111-1111-111111111111"
PROCESS = "22222222-2222-2222-2222-222222222222"
BATCH = "33333333-3333-3333-3333-333333333333"
MANAGER = "44444444-4444-4444-4444-444444444444"


def snapshot():
    return {
        "entities": [
            {"id": "s-erp", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True}},
            {"id": "p-invoicing", "kind": "process", "name": "Invoicing",
             "attributes": {"required_skills": ["approvals"]}},
            {"id": "b-march", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR"}},
            {"id": "m-finance", "kind": "person", "name": "Finance Manager",
             "attributes": {"skills": ["approvals"]}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-invoicing", "target_id": "s-erp",
             "kind": "depends_on", "confidence": 1.0},
            {"id": "e-2", "source_id": "b-march", "target_id": "p-invoicing",
             "kind": "depends_on", "confidence": 1.0},
            {"id": "e-3", "source_id": "m-finance", "target_id": "p-invoicing",
             "kind": "approves", "confidence": 1.0},
        ],
    }


def simulated():
    result = simulate(snapshot(), build_scenario("server_down", ["s-erp"]))
    return drills.normalize_simulated_steps(result), result


def drill_task(step_key, resource_id, *, status="verified", kind="recovery",
               owner_id=MANAGER, service_percent=100, recovery_minutes=45):
    task = {
        "step": {"key": step_key, "title": f"Step {step_key}", "kind": kind,
                 "owner_id": owner_id, "resource_id": resource_id,
                 "due_minutes": 60},
        "status": status,
        "overdue": False,
    }
    if status == "verified":
        task["observed"] = {"recovery_minutes": recovery_minutes,
                            "service_percent": service_percent}
    return task


def drill_report(tasks, *, mode="drill", status="closed"):
    return {
        "run": {"id": "99999999-9999-9999-9999-999999999999",
                "mode": mode, "status": status},
        "plan": {"id": "88888888-8888-8888-8888-888888888888"},
        "tasks": tasks,
        "verified_steps": sum(1 for t in tasks if t["status"] == "verified"),
        "total_steps": len(tasks),
    }


def test_exact_match_full_coverage():
    steps, result = simulated()
    tasks = [
        drill_task("restore-erp", "s-erp", status="verified"),
        drill_task("resume-invoicing", "p-invoicing", status="verified"),
        drill_task("verify-batch", "b-march", status="verified"),
    ]
    comparison = drills.compare_drill(steps, drills.normalize_drill_steps(drill_report(tasks)))
    assert comparison["summary"]["coverage"] == 1.0
    assert comparison["summary"]["missing_count"] == 0
    assert comparison["summary"]["extra_count"] == 0
    assert comparison["summary"]["owner_mismatch_count"] == 0
    assert comparison["summary"]["out_of_order_count"] == 0
    assert comparison["summary"]["drill_verified_rate"] == 1.0
    assert result["recovery_plan"]["waves"]  # the simulation actually predicted steps


def test_missing_step_listed():
    steps, _ = simulated()
    tasks = [drill_task("restore-erp", "s-erp")]
    comparison = drills.compare_drill(steps, drills.normalize_drill_steps(drill_report(tasks)))
    assert comparison["summary"]["missing_count"] == len(steps) - 1
    assert comparison["summary"]["coverage"] == 1 / len(steps)
    assert all("not practised" in f for f in comparison["findings"] if "not practised" in f)
    assert [m["entity_id"] for m in comparison["missing"]] == [
        s["entity_id"] for s in steps if s["entity_id"] != "s-erp"]


def test_extra_step_listed():
    steps, _ = simulated()
    tasks = [
        drill_task("restore-erp", "s-erp"),
        drill_task("resume-invoicing", "p-invoicing"),
        drill_task("verify-batch", "b-march"),
        drill_task("call-press", None, kind="communication"),
    ]
    comparison = drills.compare_drill(steps, drills.normalize_drill_steps(drill_report(tasks)))
    assert comparison["summary"]["extra_count"] == 1
    assert comparison["extra"][0]["step_key"] == "call-press"
    assert comparison["extra"][0]["key"] == "step:call-press"


def test_owner_mismatch_only_when_both_ids_present():
    # Simulated activate_backup step carries a person owner_id; the
    # drill used a different person -> mismatch.
    sim = [{"key": "p-invoicing", "entity_id": "p-invoicing", "entity_name": "Invoicing",
            "action": "activate_backup", "owner": "Finance Manager",
            "owner_id": MANAGER, "wave": 1, "state": "failed", "note": ""}]
    drill = [{"key": "p-invoicing", "step_key": "resume", "title": "Resume",
              "action": "recovery", "owner_id": "55555555-5555-5555-5555-555555555555",
              "owner": None, "status": "verified", "verified": True,
              "service_percent": 100, "recovery_minutes": 30, "overdue": False}]
    comparison = drills.compare_drill(sim, drill)
    assert comparison["summary"]["owner_mismatch_count"] == 1
    assert comparison["summary"]["owner_match_rate"] == 0.0

    # Same person on both sides -> match.
    drill[0]["owner_id"] = MANAGER
    assert drills.compare_drill(sim, drill)["summary"]["owner_mismatch_count"] == 0

    # Simulated owner is a display name ("operations", no id) and the
    # drill names a person: not comparable, never a mismatch.
    sim[0]["owner_id"] = None
    assert drills.compare_drill(sim, drill)["summary"]["owner_mismatch_count"] == 0


def test_order_mismatch_detected():
    sim = [
        {"key": "a", "entity_id": "a", "entity_name": "A", "action": "restore_from_backup",
         "owner": "operations", "owner_id": None, "wave": 1, "state": "failed", "note": ""},
        {"key": "b", "entity_id": "b", "entity_name": "B", "action": "manual_workaround",
         "owner": "operations", "owner_id": None, "wave": 2, "state": "degraded", "note": ""},
        {"key": "c", "entity_id": "c", "entity_name": "C", "action": "manual_workaround",
         "owner": "operations", "owner_id": None, "wave": 2, "state": "degraded", "note": ""},
    ]
    drill = [
        {"key": "b", "step_key": "b", "title": "B", "action": "recovery", "owner_id": None,
         "owner": None, "status": "verified", "verified": True,
         "service_percent": 100, "recovery_minutes": 10, "overdue": False},
        {"key": "a", "step_key": "a", "title": "A", "action": "recovery", "owner_id": None,
         "owner": None, "status": "verified", "verified": True,
         "service_percent": 100, "recovery_minutes": 20, "overdue": False},
        {"key": "c", "step_key": "c", "title": "C", "action": "recovery", "owner_id": None,
         "owner": None, "status": "verified", "verified": True,
         "service_percent": 100, "recovery_minutes": 30, "overdue": False},
    ]
    comparison = drills.compare_drill(sim, drill)
    assert comparison["out_of_order"] == ["a", "b"]
    assert comparison["summary"]["out_of_order_count"] == 2


def test_duplicate_drill_keys_only_first_matched():
    sim = [{"key": "a", "entity_id": "a", "entity_name": "A", "action": "restore_from_backup",
            "owner": "operations", "owner_id": None, "wave": 1, "state": "failed", "note": ""}]
    drill = [
        {"key": "a", "step_key": "a1", "title": "A1", "action": "recovery", "owner_id": None,
         "owner": None, "status": "verified", "verified": True,
         "service_percent": 100, "recovery_minutes": 10, "overdue": False},
        {"key": "a", "step_key": "a2", "title": "A2", "action": "recovery", "owner_id": None,
         "owner": None, "status": "verified", "verified": True,
         "service_percent": 100, "recovery_minutes": 12, "overdue": False},
    ]
    comparison = drills.compare_drill(sim, drill)
    assert comparison["summary"]["matched_count"] == 1
    assert comparison["duplicate_drill_keys"] == ["a"]
    assert any("more than once" in f for f in comparison["findings"])


def test_prefix_normalization_matches_bc_and_cg_ids():
    sim = [{"key": "bc:" + ERP, "entity_id": "bc:" + ERP, "entity_name": "ERP",
            "action": "restore_from_backup", "owner": "operations", "owner_id": None,
            "wave": 1, "state": "failed", "note": ""}]
    drill = [{"key": ERP, "step_key": "restore", "title": "Restore", "action": "recovery",
              "owner_id": None, "owner": None, "status": "verified", "verified": True,
              "service_percent": 100, "recovery_minutes": 10, "overdue": False}]
    assert drills.compare_drill(sim, drill)["summary"]["matched_count"] == 1
    sim[0]["key"] = "cg:" + ERP
    sim[0]["entity_id"] = "cg:" + ERP
    assert drills.compare_drill(sim, drill)["summary"]["matched_count"] == 1


def test_empty_simulation_and_empty_drill():
    comparison = drills.compare_drill([], [])
    assert comparison["summary"]["coverage"] is None
    assert comparison["summary"]["matched_count"] == 0
    assert "nothing to compare" in comparison["findings"][0]

    steps, _ = simulated()
    comparison = drills.compare_drill(steps, [])
    assert comparison["summary"]["coverage"] == 0.0
    assert comparison["summary"]["missing_count"] == len(steps)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        drills.normalize_simulated_steps({"recovery_plan": {"waves": "nope"}})
    with pytest.raises(ValueError):
        drills.normalize_simulated_steps({"recovery_plan": {"waves": [[{"action": "x"}]]}})
    with pytest.raises(ValueError):
        drills.normalize_drill_steps({"tasks": [{"status": "verified"}]})
    with pytest.raises(ValueError):
        drills.normalize_drill_steps({"tasks": "nope"})
    with pytest.raises(ValueError):
        drills.compare_drill([{"key": ""}], [])
    with pytest.raises(ValueError):
        drills.compare_drill_to_simulation({}, {"tasks": []})


def test_inputs_are_not_mutated():
    steps, result = simulated()
    tasks = [drill_task("restore-erp", "s-erp"),
             drill_task("resume-invoicing", "p-invoicing"),
             drill_task("verify-batch", "b-march")]
    report = drill_report(tasks)
    steps_before, result_before, report_before = (
        copy.deepcopy(steps), copy.deepcopy(result), copy.deepcopy(report))
    drills.compare_drill_to_simulation(result, report)
    assert steps == steps_before
    assert result == result_before
    assert report == report_before


def test_end_to_end_simulation_vs_mirrored_drill():
    steps, result = simulated()
    # A drill that practised exactly the predicted recovery, step for
    # step, in wave order, with matching backup owner where the
    # simulation named one.
    tasks = []
    for index, step in enumerate(steps):
        tasks.append(drill_task(f"step-{index}", step["entity_id"],
                                owner_id=step["owner_id"] or MANAGER))
    comparison = drills.compare_drill_to_simulation(
        result, drill_report(tasks))
    assert comparison["summary"]["coverage"] == 1.0
    assert comparison["summary"]["owner_mismatch_count"] == 0
    assert comparison["summary"]["out_of_order_count"] == 0
    assert comparison["scenario"]["kind"] == "system_outage"
    assert comparison["run"]["mode"] == "drill"

    # The same drill, but the first predicted step was never done.
    tasks = tasks[1:]
    comparison = drills.compare_drill_to_simulation(
        result, drill_report(tasks))
    assert comparison["summary"]["missing_count"] == 1
    assert comparison["missing"][0]["entity_id"] == steps[0]["entity_id"]


def test_incident_mode_is_flagged_not_rejected():
    steps, result = simulated()
    tasks = [drill_task("restore-erp", "s-erp")]
    comparison = drills.compare_drill_to_simulation(
        result, drill_report(tasks, mode="incident"))
    assert comparison["run"]["mode"] == "incident"
    assert "real incident" in comparison["findings"][0]


def test_unverified_drill_steps_counted_not_hidden():
    steps, _ = simulated()
    tasks = [
        drill_task("restore-erp", "s-erp", status="verified"),
        drill_task("resume-invoicing", "p-invoicing", status="pending"),
        drill_task("verify-batch", "b-march", status="evidence_submitted"),
    ]
    normalized = drills.normalize_drill_steps(drill_report(tasks))
    assert [t["verified"] for t in normalized] == [True, False, False]
    assert normalized[1]["recovery_minutes"] is None
    assert normalized[1]["service_percent"] is None
    comparison = drills.compare_drill(steps, normalized)
    assert comparison["summary"]["drill_verified_rate"] == pytest.approx(1 / 3)
