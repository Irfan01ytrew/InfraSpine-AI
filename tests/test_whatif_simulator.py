"""Tests for the Step AL what-if scenario simulator.

Pure-function tests over synthetic snapshots. No database rows are
touched; run with ``--noconftest`` because the repo conftest needs the
full app stack. Log capture stays on.
"""

import pytest

from app.whatif.simulator import (
    SCENARIOS,
    SCENARIO_KINDS,
    build_scenario,
    list_templates,
    money_at_risk,
    simulate,
    template_name,
)


def _snapshot():
    """A small company: people, team, systems, suppliers, process, money.

    - Ayesha (finance manager) approves batch b-1, operates ERP, owns process.
    - Bilal is the recorded backup for b-1 and holds the approvals skill.
    - ERP serves + is depended on by the pay process; ERP depends on the DB.
    - Two suppliers supply the pay process (Acme + Globex).
    - The DB documents the vendor-contracts data node.
    """
    return {
        "entities": [
            {"id": "p-ayesha", "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["approvals", "invoicing"]}},
            {"id": "p-bilal", "kind": "person", "name": "Bilal",
             "attributes": {"skills": ["approvals"]}},
            {"id": "p-chloe", "kind": "person", "name": "Chloe",
             "attributes": {"skills": ["invoicing"]}},
            {"id": "t-finance", "kind": "team", "name": "Finance"},
            {"id": "s-erp", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True}},
            {"id": "s-db", "kind": "system", "name": "Database",
             "attributes": {"restorable_from_backup": True}},
            {"id": "sup-acme", "kind": "supplier", "name": "Acme"},
            {"id": "sup-globex", "kind": "supplier", "name": "Globex"},
            {"id": "proc-pay", "kind": "process", "name": "Pay invoices"},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 150000, "currency": "PKR",
                            "required_skills": ["approvals"]}},
            {"id": "pay-1", "kind": "payment", "name": "March payment",
             "attributes": {"amount": 50000, "currency": "PKR"}},
            {"id": "d-1", "kind": "data", "name": "Vendor contracts"},
            {"id": "site-hq", "kind": "site", "name": "Head office"},
        ],
        "relationships": [
            {"id": "e1", "source_id": "p-ayesha", "target_id": "b-1", "kind": "approves", "confidence": 1.0},
            {"id": "e2", "source_id": "p-ayesha", "target_id": "s-erp", "kind": "operates", "confidence": 1.0},
            {"id": "e3", "source_id": "p-ayesha", "target_id": "proc-pay", "kind": "owns", "confidence": 1.0},
            {"id": "e4", "source_id": "p-bilal", "target_id": "b-1", "kind": "backup_for", "confidence": 1.0},
            {"id": "e5", "source_id": "s-erp", "target_id": "proc-pay", "kind": "serves", "confidence": 1.0},
            {"id": "e6", "source_id": "proc-pay", "target_id": "s-erp", "kind": "depends_on", "confidence": 1.0},
            {"id": "e7", "source_id": "s-erp", "target_id": "s-db", "kind": "depends_on", "confidence": 1.0},
            {"id": "e8", "source_id": "b-1", "target_id": "s-erp", "kind": "depends_on", "confidence": 1.0},
            {"id": "e9", "source_id": "pay-1", "target_id": "b-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "e10", "source_id": "sup-acme", "target_id": "proc-pay", "kind": "supplies", "confidence": 1.0},
            {"id": "e11", "source_id": "sup-globex", "target_id": "proc-pay", "kind": "supplies", "confidence": 1.0},
            {"id": "e12", "source_id": "s-erp", "target_id": "d-1", "kind": "documents", "confidence": 1.0},
            {"id": "e13", "source_id": "p-ayesha", "target_id": "t-finance", "kind": "member_of", "confidence": 1.0},
            {"id": "e14", "source_id": "p-bilal", "target_id": "t-finance", "kind": "member_of", "confidence": 1.0},
            {"id": "e15", "source_id": "proc-pay", "target_id": "site-hq", "kind": "located_at", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _scenario(kind, seed_ids, horizon_days=7):
    return {"kind": kind, "seed_ids": list(seed_ids), "horizon_days": horizon_days}


def _by_id(result):
    return {entry["entity"]["id"]: entry for entry in result["affected"]}


# ---------------------------------------------------------------------------
# Scenario library
# ---------------------------------------------------------------------------


def test_build_scenario_uses_template_defaults():
    scenario = build_scenario("server_down", ["s-erp"])
    assert scenario["kind"] == "system_outage"
    assert scenario["name"] == "Server down"
    assert scenario["seed_ids"] == ["s-erp"]
    assert scenario["horizon_days"] == 1  # template default


def test_build_scenario_horizon_override():
    scenario = build_scenario("supplier_fails", ["sup-acme"], horizon_days=30)
    assert scenario["horizon_days"] == 30


def test_build_scenario_rejects_unknown_template():
    with pytest.raises(ValueError, match="Unknown scenario template"):
        build_scenario("meteor_strike", ["s-erp"])


def test_build_scenario_rejects_empty_and_duplicate_seeds():
    with pytest.raises(ValueError, match="non-empty"):
        build_scenario("server_down", [])
    with pytest.raises(ValueError, match="duplicates"):
        build_scenario("server_down", ["s-erp", "s-erp"])


def test_build_scenario_rejects_bad_horizon():
    with pytest.raises(ValueError, match="horizon_days"):
        build_scenario("server_down", ["s-erp"], horizon_days=0)


def test_all_library_templates_are_known_kinds():
    for template_id, template in SCENARIOS.items():
        assert template["kind"] in {
            "employee_absence", "team_absence", "system_outage",
            "supplier_failure", "cyber_incident", "site_unavailable", "compound",
        }, template_id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_simulate_rejects_unknown_kind():
    with pytest.raises(ValueError, match="Unknown scenario kind"):
        simulate(_snapshot(), _scenario("alien_invasion", ["s-erp"]))


def test_simulate_rejects_seed_kind_mismatch():
    with pytest.raises(ValueError, match="cannot seed kind"):
        simulate(_snapshot(), _scenario("system_outage", ["p-ayesha"]))


def test_simulate_rejects_unknown_seed():
    with pytest.raises(ValueError, match="Unknown seed id"):
        simulate(_snapshot(), _scenario("system_outage", ["s-nope"]))


def test_simulate_rejects_empty_seeds_and_bad_numbers():
    with pytest.raises(ValueError, match="non-empty"):
        simulate(_snapshot(), _scenario("system_outage", []))
    with pytest.raises(ValueError, match="horizon_days"):
        simulate(_snapshot(), _scenario("system_outage", ["s-erp"], horizon_days=0))
    with pytest.raises(ValueError, match="depth"):
        simulate(_snapshot(), _scenario("system_outage", ["s-erp"]), depth=0)


def test_simulate_rejects_bad_snapshot():
    bad = _snapshot()
    bad["entities"].append({"id": "s-erp", "kind": "system", "name": "Dupe"})
    with pytest.raises(ValueError, match="Duplicate entity id"):
        simulate(bad, _scenario("system_outage", ["s-erp"]))
    bad = _snapshot()
    bad["relationships"].append(
        {"id": "ex", "source_id": "s-erp", "target_id": "ghost", "kind": "depends_on"}
    )
    with pytest.raises(ValueError, match="unknown target_id"):
        simulate(bad, _scenario("system_outage", ["s-erp"]))
    bad = _snapshot()
    bad["relationships"][0]["confidence"] = 1.5
    with pytest.raises(ValueError, match="confidence"):
        simulate(bad, _scenario("system_outage", ["s-erp"]))


def test_money_entity_requires_non_negative_amount():
    bad = _snapshot()
    for entity in bad["entities"]:
        if entity["id"] == "b-1":
            entity["attributes"]["amount"] = -5
    with pytest.raises(ValueError, match="must be >= 0"):
        simulate(bad, _scenario("system_outage", ["s-erp"]))


# ---------------------------------------------------------------------------
# Founder questions
# ---------------------------------------------------------------------------


def test_founder_question_finance_manager_removed():
    result = simulate(_snapshot(), build_scenario("key_employee_leaves", ["p-ayesha"]))
    by_id = _by_id(result)
    assert result["scenario"]["kind"] == "employee_absence"
    assert result["scenario"]["seed_names"] == ["Ayesha"]
    # What Ayesha holds fails with her.
    assert by_id["b-1"]["state"] == "failed"
    assert by_id["s-erp"]["state"] == "failed"
    assert by_id["proc-pay"]["state"] == "failed"
    # Downstream of the failed batch: the payment fails too.
    assert by_id["pay-1"]["state"] == "failed"
    # Bilal (not absent) is unaffected.
    assert "p-bilal" not in by_id
    # Money at risk is exposure, per currency.
    assert result["money_at_risk"]["totals"] == {"PKR": 200000.0}
    assert result["money_at_risk"]["batch_count"] == 1
    assert result["money_at_risk"]["payment_count"] == 1
    # The batch is critical (failed + money); the process is critical too.
    assert by_id["b-1"]["severity"] == "critical"
    assert by_id["proc-pay"]["severity"] == "critical"
    assert result["dry_run"] is True


def test_founder_question_erp_down():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-erp"]))
    by_id = _by_id(result)
    assert by_id["s-erp"]["state"] == "failed"
    assert by_id["proc-pay"]["state"] == "failed"
    assert by_id["b-1"]["state"] == "failed"
    assert by_id["pay-1"]["state"] == "failed"
    # The DB is upstream of ERP, not downstream: unaffected.
    assert "s-db" not in by_id
    # The batch has a recorded backup (Bilal, holds approvals).
    batch = by_id["b-1"]
    assert batch["recovery_options"][0]["action"] == "activate_backup"
    assert batch["recovery_options"][0]["owner"] == "Bilal"


def test_founder_question_supplier_fails():
    result = simulate(_snapshot(), build_scenario("supplier_fails", ["sup-acme"]))
    by_id = _by_id(result)
    assert by_id["sup-acme"]["state"] == "failed"
    # Globex still supplies the process: it degrades, it does not fail.
    assert by_id["proc-pay"]["state"] == "degraded"
    assert by_id["proc-pay"]["severity"] == "medium"
    # The process has an alternate supplier as its best recovery option.
    assert by_id["proc-pay"]["recovery_options"][0]["action"] == "alternate_supplier"
    assert by_id["proc-pay"]["recovery_options"][0]["owner"] == "Globex"


def test_both_suppliers_failing_fails_the_process():
    result = simulate(
        _snapshot(),
        _scenario("compound", ["sup-acme", "sup-globex"], horizon_days=14),
    )
    by_id = _by_id(result)
    assert by_id["proc-pay"]["state"] == "failed"
    assert "p-ayesha" not in by_id


# ---------------------------------------------------------------------------
# Other scenario kinds
# ---------------------------------------------------------------------------


def test_cyber_incident_exposes_data_and_degrades_dependents():
    result = simulate(_snapshot(), build_scenario("cyber_incident", ["s-erp"]))
    by_id = _by_id(result)
    assert by_id["s-erp"]["state"] == "failed"
    # Data documented by the compromised system is exposed, not failed.
    assert by_id["d-1"]["state"] == "exposed"
    assert by_id["d-1"]["severity"] == "low"
    # The process depends on the compromised system: degraded (quarantine).
    assert by_id["proc-pay"]["state"] == "degraded"
    # The batch depends on ERP too: degraded, not failed.
    assert by_id["b-1"]["state"] == "degraded"


def test_team_absence_expands_to_members():
    result = simulate(_snapshot(), _scenario("team_absence", ["t-finance"]))
    by_id = _by_id(result)
    assert result["scenario"]["seed_names"] == ["Finance"]
    # Both members are absent, so what they hold fails.
    assert by_id["b-1"]["state"] == "failed"
    assert by_id["s-erp"]["state"] == "failed"
    # Bilal is absent too: no eligible backup for the batch.
    assert by_id["b-1"]["recovery_options"][0]["action"] != "activate_backup"
    uncovered = {u["entity"]["id"]: u["reason"] for u in result["recovery_plan"]["uncovered"]}
    assert uncovered["b-1"] == "backup_unavailable"


def test_site_unavailable_propagates_via_located_at():
    result = simulate(_snapshot(), build_scenario("site_unavailable", ["site-hq"]))
    by_id = _by_id(result)
    assert by_id["site-hq"]["state"] == "failed"
    assert by_id["proc-pay"]["state"] == "failed"


def test_compound_scenario_mixes_kinds():
    result = simulate(
        _snapshot(),
        _scenario("compound", ["s-db", "p-ayesha"], horizon_days=7),
    )
    by_id = _by_id(result)
    assert by_id["s-db"]["state"] == "failed"
    assert by_id["s-erp"]["state"] == "failed"
    assert by_id["b-1"]["state"] == "failed"
    assert by_id["pay-1"]["state"] == "failed"


# ---------------------------------------------------------------------------
# Recovery options, waves, rehearsal
# ---------------------------------------------------------------------------


def test_backup_missing_skills_is_reported_not_bridged():
    snapshot = _snapshot()
    for entity in snapshot["entities"]:
        if entity["id"] == "p-bilal":
            entity["attributes"]["skills"] = ["invoicing"]  # no approvals
    result = simulate(snapshot, build_scenario("key_employee_leaves", ["p-ayesha"]))
    by_id = _by_id(result)
    assert by_id["b-1"]["recovery_options"][0]["action"] != "activate_backup"
    uncovered = {u["entity"]["id"]: u["reason"] for u in result["recovery_plan"]["uncovered"]}
    assert uncovered["b-1"] == "backup_missing_skills"


def test_restore_from_backup_option_for_systems():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-db"]))
    by_id = _by_id(result)
    # The DB itself can be restored from backup.
    actions = [o["action"] for o in by_id["s-db"]["recovery_options"]]
    assert "restore_from_backup" in actions
    # ERP depends on the DB, so it fails too, and it is also restorable.
    assert by_id["s-erp"]["state"] == "failed"
    assert "restore_from_backup" in [o["action"] for o in by_id["s-erp"]["recovery_options"]]


def test_manual_workaround_is_always_the_fallback():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-erp"]))
    by_id = _by_id(result)
    for entry in result["affected"]:
        assert entry["recovery_options"][-1]["action"] == "manual_workaround"
    # The payment has no backup, no alternate, no restore: uncovered.
    uncovered = {u["entity"]["id"]: u["reason"] for u in result["recovery_plan"]["uncovered"]}
    assert uncovered["pay-1"] == "no_alternate_supplier"


def test_recovery_waves_respect_dependencies():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-erp"]))
    waves = result["recovery_plan"]["waves"]
    flat = [step["entity_id"] for wave in waves for step in wave]
    assert set(flat) == set(result["failed"]) | set(result["degraded"]) | set(result["exposed"])
    # The batch depends on ERP, so ERP recovers in an earlier wave.
    wave_of = {step["entity_id"]: i for i, wave in enumerate(waves) for step in wave}
    assert wave_of["s-erp"] < wave_of["b-1"]
    assert wave_of["b-1"] < wave_of["pay-1"]
    # Every wave step names its action and owner.
    for wave in waves:
        for step in wave:
            assert step["action"] in {
                "activate_backup", "alternate_supplier",
                "restore_from_backup", "manual_workaround",
            }
            assert step["owner"]


def test_rehearsal_rewrites_plan_as_practice_only_steps():
    result = simulate(
        _snapshot(), build_scenario("server_down", ["s-erp"]), rehearse=True
    )
    rehearsal = result["rehearsal"]
    assert rehearsal is not None
    assert rehearsal["dry_run"] is True
    assert rehearsal["side_effects"] == []
    steps = rehearsal["steps"]
    assert [s["number"] for s in steps] == list(range(1, len(steps) + 1))
    assert all(s["practice_only"] for s in steps)
    # Same entities, same order as the recovery waves.
    flat = [s["entity_id"] for wave in result["recovery_plan"]["waves"] for s in wave]
    assert [s["entity_id"] for s in steps] == flat


def test_no_rehearsal_block_without_rehearse():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-erp"]))
    assert result["rehearsal"] is None
    assert result["dry_run"] is True


def test_deterministic_for_same_snapshot_and_scenario():
    snapshot = _snapshot()
    scenario = build_scenario("server_down", ["s-erp"])
    first = simulate(snapshot, scenario, rehearse=True)
    second = simulate(snapshot, scenario, rehearse=True)
    assert first == second


# ---------------------------------------------------------------------------
# Propagation rules
# ---------------------------------------------------------------------------


def test_depth_limit_truncates_and_reports():
    result = simulate(
        _snapshot(), build_scenario("server_down", ["s-erp"]), depth=1
    )
    by_id = _by_id(result)
    assert result["truncated"] is True
    # Depth 1: only direct neighbours of the seed are affected.
    assert "s-erp" in by_id and "proc-pay" in by_id and "b-1" in by_id
    assert "pay-1" not in by_id


def test_cycle_does_not_loop_and_is_reported():
    snapshot = _snapshot()
    snapshot["relationships"].append(
        {"id": "e16", "source_id": "s-erp", "target_id": "proc-pay", "kind": "depends_on", "confidence": 1.0}
    )
    result = simulate(snapshot, build_scenario("server_down", ["s-erp"]))
    by_id = _by_id(result)
    assert by_id["proc-pay"]["state"] == "failed"
    # ERP and the process depend on each other: both are blocked in waves.
    blocked = {e["id"] for e in result["recovery_plan"]["blocked_by_dependency_cycle"]}
    assert {"s-erp", "proc-pay"} <= blocked


def test_confidence_is_the_weakest_link_on_the_path():
    snapshot = _snapshot()
    for edge in snapshot["relationships"]:
        if edge["id"] == "e8":  # batch depends_on ERP
            edge["confidence"] = 0.5
    result = simulate(snapshot, build_scenario("server_down", ["s-erp"]))
    by_id = _by_id(result)
    # The batch's recorded path is the single 0.5 edge.
    assert by_id["b-1"]["confidence"] == 0.5
    # The payment's recorded path runs through the batch: min(0.5, 1.0).
    assert by_id["pay-1"]["confidence"] == 0.5
    # The process is reached first via the serves edge (1.0), which is
    # the recorded path — confidence follows the recorded path, not the
    # weakest of all possible paths.
    assert by_id["proc-pay"]["confidence"] == 1.0


def test_paths_explain_why_each_entity_is_affected():
    result = simulate(_snapshot(), build_scenario("server_down", ["s-erp"]))
    by_id = _by_id(result)
    pay_path = by_id["pay-1"]["path"]
    kinds = [step["kind"] for step in pay_path]
    assert kinds == ["depends_on", "depends_on"]
    # Paths are stored seed-outward: first the edge nearest the seed.
    assert pay_path[0]["source_id"] == "b-1"
    assert pay_path[0]["target_id"] == "s-erp"
    assert pay_path[1]["source_id"] == "pay-1"
    assert pay_path[1]["target_id"] == "b-1"


def test_money_at_risk_helper_sums_per_currency():
    entities = [
        {"id": "b-1", "kind": "invoice_batch", "attributes": {"amount": 100, "currency": "PKR"}},
        {"id": "b-2", "kind": "invoice_batch", "attributes": {"amount": 50, "currency": "USD"}},
        {"id": "pay-1", "kind": "payment", "attributes": {"amount": 25, "currency": "PKR"}},
        {"id": "p-1", "kind": "person", "attributes": {}},
    ]
    assert money_at_risk(entities) == {
        "totals": {"PKR": 125.0, "USD": 50.0},
        "batch_count": 2,
        "payment_count": 1,
    }
    assert money_at_risk([]) == {"totals": {}, "batch_count": 0, "payment_count": 0}


def test_list_templates_covers_every_scenario_kind():
    templates = list_templates()
    assert {t["template_id"] for t in templates} == set(SCENARIOS)
    for template in templates:
        assert template["kind"] in SCENARIO_KINDS
        assert template["name"]
        assert template["description"]
        assert template["horizon_days"] >= 1


def test_template_name_lookup():
    assert template_name("server_down") == "Server down"
    assert template_name("no_such_template") is None
