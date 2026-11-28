"""Digital twin slice 1: person-absence dependency-impact simulation.

Pure-function tests over synthetic snapshots. No database rows are
created by these tests themselves; the repo conftest session fixture
still migrates the dedicated *_test database, as for every test module.
"""

import pytest

from app.digital_twin.impact import DEFAULT_DEPTH, DEFAULT_HORIZON_DAYS, money_at_risk, simulate_absence


def person(pid, name, skills=()):
    return {"id": pid, "kind": "person", "name": name, "attributes": {"skills": list(skills)}}


def entity(eid, kind, name, attributes=None):
    return {"id": eid, "kind": kind, "name": name, "attributes": dict(attributes or {})}


def edge(eid, source, target, kind, confidence=1.0):
    return {"id": eid, "source_id": source, "target_id": target, "kind": kind,
            "confidence": confidence, "evidence": []}


def snapshot(entities, relationships, **extra):
    data = {"entities": entities, "relationships": relationships, "as_of": "2026-11-28T00:00:00+00:00"}
    data.update(extra)
    return data


def affected_ids(result):
    return [entry["entity"]["id"] for entry in result["affected"]]


def by_id(result, eid):
    return next(entry for entry in result["affected"] if entry["entity"]["id"] == eid)


def test_direct_authority_is_at_risk_distance_one():
    snap = snapshot(
        [person("p-1", "Ayesha"), entity("b-1", "invoice_batch", "March batch",
                                        {"amount": 1500.0, "currency": "PKR"})],
        [edge("e-1", "p-1", "b-1", "approves")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert affected_ids(result) == ["b-1"]
    assert by_id(result, "b-1")["distance"] == 1
    assert by_id(result, "b-1")["confidence"] == 1.0
    assert result["absent_names"] == ["Ayesha"]
    assert result["horizon_days"] == DEFAULT_HORIZON_DAYS
    assert result["as_of"] == "2026-11-28T00:00:00+00:00"


def test_impact_propagates_through_depends_on():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("pr-1", "process", "Payroll"),
         entity("w-1", "workflow", "Salary run"),
         entity("w-2", "workflow", "Bank file")],
        [edge("e-1", "p-1", "pr-1", "owns"),
         edge("e-2", "w-1", "pr-1", "depends_on"),
         edge("e-3", "w-2", "w-1", "depends_on")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert affected_ids(result) == ["pr-1", "w-1", "w-2"]
    assert by_id(result, "w-1")["distance"] == 2
    assert by_id(result, "w-2")["distance"] == 3
    assert by_id(result, "w-2")["path"][-1]["kind"] == "depends_on"


def test_impact_propagates_through_supplies():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("s-1", "system", "Payroll system"),
         entity("f-1", "process", "Salary feed")],
        [edge("e-1", "p-1", "s-1", "operates"),
         edge("e-2", "s-1", "f-1", "supplies")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert affected_ids(result) == ["s-1", "f-1"]


def test_team_absence_expands_to_members():
    snap = snapshot(
        [person("p-1", "Ayesha"), person("p-2", "Bilal"), person("p-3", "Sara"),
         entity("t-1", "team", "Finance"),
         entity("b-1", "invoice_batch", "March batch", {"amount": 10.0, "currency": "PKR"}),
         entity("b-2", "invoice_batch", "April batch", {"amount": 20.0, "currency": "PKR"})],
        [edge("e-1", "p-1", "t-1", "member_of"),
         edge("e-2", "p-2", "t-1", "member_of"),
         edge("e-3", "p-1", "b-1", "approves"),
         edge("e-4", "p-2", "b-2", "approves")],
    )
    result = simulate_absence(snap, ["t-1"])
    # Both at distance 1; affected is ordered by (distance, name), so
    # "April batch" (b-2) sorts before "March batch" (b-1).
    assert affected_ids(result) == ["b-2", "b-1"]
    assert result["absent_person_ids"] == ["p-1", "p-2"]
    # Sara is not in the team: her work is untouched.
    assert "p-3" not in result["absent_person_ids"]


def test_unknown_absent_id_rejected():
    snap = snapshot([person("p-1", "Ayesha")], [])
    with pytest.raises(ValueError, match="Unknown absent id"):
        simulate_absence(snap, ["p-9"])


def test_non_person_absent_rejected():
    snap = snapshot(
        [person("p-1", "Ayesha"), entity("s-1", "system", "ERP")],
        [edge("e-1", "p-1", "s-1", "operates")],
    )
    with pytest.raises(ValueError, match="only 'person' or 'team'"):
        simulate_absence(snap, ["s-1"])


def test_empty_and_duplicate_absent_ids_rejected():
    snap = snapshot([person("p-1", "Ayesha")], [])
    with pytest.raises(ValueError, match="non-empty"):
        simulate_absence(snap, [])
    with pytest.raises(ValueError, match="duplicates"):
        simulate_absence(snap, ["p-1", "p-1"])


def test_invalid_horizon_and_depth_rejected():
    snap = snapshot([person("p-1", "Ayesha")], [])
    with pytest.raises(ValueError, match="horizon_days"):
        simulate_absence(snap, ["p-1"], horizon_days=0)
    with pytest.raises(ValueError, match="depth"):
        simulate_absence(snap, ["p-1"], depth=0)


def test_money_at_risk_totals_per_currency():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("b-1", "invoice_batch", "March", {"amount": 1500.0, "currency": "PKR"}),
         entity("b-2", "invoice_batch", "April", {"amount": 250.5, "currency": "PKR"}),
         entity("pay-1", "payment", "Pay 1", {"amount": 100.0, "currency": "USD"}),
         entity("pr-1", "process", "Payroll")],
        [edge("e-1", "p-1", "b-1", "approves"),
         edge("e-2", "p-1", "b-2", "approves"),
         edge("e-3", "p-1", "pay-1", "approves"),
         edge("e-4", "p-1", "pr-1", "owns")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert result["money_at_risk"] == {
        "totals": {"PKR": 1750.5, "USD": 100.0},
        "batch_count": 2,
        "payment_count": 1,
    }
    # Unaffected money is never counted.
    assert result["money_at_risk"]["totals"]["PKR"] == 1750.5


def test_money_helper_directly_and_missing_amount_rejected():
    entities = [
        entity("b-1", "invoice_batch", "March", {"amount": 5, "currency": "PKR"}),
        entity("pr-1", "process", "Payroll"),
    ]
    assert money_at_risk(entities) == {"totals": {"PKR": 5.0}, "batch_count": 1, "payment_count": 0}
    with pytest.raises(ValueError, match="needs attributes.amount"):
        simulate_absence(
            snapshot([person("p-1", "Ayesha"), entity("b-1", "invoice_batch", "March", {})],
                     [edge("e-1", "p-1", "b-1", "approves")]),
            ["p-1"],
        )
    with pytest.raises(ValueError, match="must be >= 0"):
        money_at_risk([entity("b-1", "invoice_batch", "March", {"amount": -1})])


def test_backup_with_skills_covers():
    snap = snapshot(
        [person("p-1", "Ayesha"), person("p-2", "Bilal", skills=["approvals"]),
         entity("b-1", "invoice_batch", "March", {"amount": 10.0, "currency": "PKR",
                                                 "required_skills": ["approvals"]})],
        [edge("e-1", "p-1", "b-1", "approves"),
         edge("e-2", "p-2", "b-1", "backup_for")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert by_id(result, "b-1")["covered_by"] == [{"person_id": "p-2", "name": "Bilal"}]
    assert result["uncovered"] == []


def test_backup_absent_is_uncovered():
    snap = snapshot(
        [person("p-1", "Ayesha"), person("p-2", "Bilal", skills=["approvals"]),
         entity("b-1", "invoice_batch", "March", {"amount": 10.0, "currency": "PKR",
                                                 "required_skills": ["approvals"]})],
        [edge("e-1", "p-1", "b-1", "approves"),
         edge("e-2", "p-2", "b-1", "backup_for")],
    )
    result = simulate_absence(snap, ["p-1", "p-2"])
    assert result["uncovered"][0]["reason"] == "backup_absent"


def test_backup_missing_skills_is_uncovered():
    snap = snapshot(
        [person("p-1", "Ayesha"), person("p-2", "Bilal", skills=["data_entry"]),
         entity("b-1", "invoice_batch", "March", {"amount": 10.0, "currency": "PKR",
                                                 "required_skills": ["approvals"]})],
        [edge("e-1", "p-1", "b-1", "approves"),
         edge("e-2", "p-2", "b-1", "backup_for")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert result["uncovered"][0]["reason"] == "backup_missing_skills"


def test_no_backup_recorded_is_uncovered():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("pr-1", "process", "Payroll")],
        [edge("e-1", "p-1", "pr-1", "owns")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert result["uncovered"][0]["reason"] == "no_backup_recorded"


def test_confidence_is_lowest_on_path():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("pr-1", "process", "Payroll"),
         entity("w-1", "workflow", "Salary run")],
        [edge("e-1", "p-1", "pr-1", "owns", confidence=0.9),
         edge("e-2", "w-1", "pr-1", "depends_on", confidence=0.5)],
    )
    result = simulate_absence(snap, ["p-1"])
    assert by_id(result, "pr-1")["confidence"] == 0.9
    assert by_id(result, "w-1")["confidence"] == 0.5


def test_depth_limit_truncates_and_reports():
    entities = [person("p-1", "Ayesha")]
    for i in range(1, 6):
        entities.append(entity(f"w-{i}", "workflow", f"Step {i}"))
    # Chain: w-1 owned by the person, w-2 depends_on w-1, w-3 on w-2, ...
    relationships = [edge("e-1", "p-1", "w-1", "owns")]
    for i in range(2, 6):
        relationships.append(edge(f"e-{i}", f"w-{i}", f"w-{i-1}", "depends_on"))
    snap = snapshot(entities, relationships)
    result = simulate_absence(snap, ["p-1"], depth=2)
    assert affected_ids(result) == ["w-1", "w-2"]
    assert result["truncated"] is True


def test_dependency_cycle_is_reported_not_hung():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("w-1", "workflow", "A"),
         entity("w-2", "workflow", "B")],
        [edge("e-1", "p-1", "w-1", "owns"),
         edge("e-2", "w-1", "w-2", "depends_on"),
         edge("e-3", "w-2", "w-1", "depends_on")],
    )
    result = simulate_absence(snap, ["p-1"])
    assert set(affected_ids(result)) == {"w-1", "w-2"}
    assert {e["id"] for e in result["blocked_by_dependency_cycle"]} == {"w-1", "w-2"}
    assert result["recovery_waves"] == []


def test_recovery_waves_respect_dependencies():
    snap = snapshot(
        [person("p-1", "Ayesha"),
         entity("pr-1", "process", "Payroll"),
         entity("w-1", "workflow", "Salary run"),
         entity("w-2", "workflow", "Bank file")],
        [edge("e-1", "p-1", "pr-1", "owns"),
         edge("e-2", "w-1", "pr-1", "depends_on"),
         edge("e-3", "w-2", "w-1", "depends_on")],
    )
    result = simulate_absence(snap, ["p-1"])
    waves = [[e["id"] for e in wave] for wave in result["recovery_waves"]]
    assert waves == [["pr-1"], ["w-1"], ["w-2"]]


def test_simulation_is_deterministic():
    snap = snapshot(
        [person("p-1", "Ayesha"), person("p-2", "Bilal", skills=["approvals"]),
         entity("b-1", "invoice_batch", "March", {"amount": 10.0, "currency": "PKR"}),
         entity("pr-1", "process", "Payroll")],
        [edge("e-1", "p-1", "b-1", "approves"),
         edge("e-2", "p-2", "b-1", "backup_for"),
         edge("e-3", "p-1", "pr-1", "owns")],
    )
    first = simulate_absence(snap, ["p-1"])
    second = simulate_absence(snap, ["p-1"])
    assert first == second


def test_result_states_simulation_only_assumptions():
    snap = snapshot([person("p-1", "Ayesha")], [])
    result = simulate_absence(snap, ["p-1"], horizon_days=7)
    assert result["affected"] == []
    assert result["money_at_risk"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    assert any("Simulation only" in a for a in result["assumptions"])
    assert any("never approves" in a for a in result["assumptions"])


def test_invalid_snapshot_rejected():
    with pytest.raises(ValueError, match="Duplicate entity id"):
        simulate_absence(
            snapshot([person("p-1", "Ayesha"), person("p-1", "Bilal")], []), ["p-1"])
    with pytest.raises(ValueError, match="unknown target_id"):
        simulate_absence(
            snapshot([person("p-1", "Ayesha")],
                     [edge("e-1", "p-1", "ghost", "owns")]),
            ["p-1"],
        )
    with pytest.raises(ValueError, match="confidence must be between"):
        simulate_absence(
            snapshot([person("p-1", "Ayesha"),
                      entity("pr-1", "process", "Payroll")],
                     [edge("e-1", "p-1", "pr-1", "owns", confidence=1.5)]),
            ["p-1"],
        )


def test_default_depth_matches_company_graph():
    assert DEFAULT_DEPTH == 6
    assert DEFAULT_HORIZON_DAYS == 7
