"""Step G slice 7: disruption drills — pure logic, no database."""

import uuid

from app.continuity.drills import DRILL_KINDS, compare_drill_to_simulation, pick_drill_targets


def _node(node_id, kind, name="Node", data=None):
    return {"id": str(node_id), "kind": kind, "name": name, "data": data or {}}


def test_pick_drill_targets_uses_graph():
    manager, server, supplier = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    targets = pick_drill_targets([
        _node(manager, "employee", "Manager"),
        _node(server, "system", "Critical Database Server", {"critical": True}),
        _node(supplier, "supplier", "Vendor"),
    ])
    assert targets["resignation"] == str(manager)
    assert targets["absence"] == str(manager)
    assert targets["server_outage"] == str(server)
    assert targets["database_failure"] == str(server)
    assert targets["supplier_disruption"] == str(supplier)
    assert set(targets) == set(DRILL_KINDS)


def test_pick_drill_targets_skips_missing_kinds():
    targets = pick_drill_targets([_node(uuid.uuid4(), "employee", "Only Employee")])
    assert targets["supplier_disruption"] is None
    assert targets["server_outage"] is None


def test_compare_matches_when_drill_beats_forecast():
    server = str(uuid.uuid4())
    result = compare_drill_to_simulation(
        {"affected_ids": [server], "recovered_ids": [server],
         "recovery_minutes": {server: 30}, "rto_minutes": {server: 60},
         "mtpd_minutes": {server: 240}, "data_loss_minutes": {server: 5}, "rpo_minutes": {server: 10}},
        {"affected_ids": [server], "forecast": {server: {"rto_exceeded": True, "mtpd_exceeded": False}}})
    assert result["matches"] is False  # drill met an RTO the simulation said would be exceeded
    assert [d["code"] for d in result["differences"]] == ["drill_met_rto_simulation_missed"]


def test_compare_reports_every_difference_code():
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    result = compare_drill_to_simulation(
        {"affected_ids": [a, b], "recovered_ids": [b, c],
         "recovery_minutes": {b: 90, c: 300}, "rto_minutes": {b: 60, c: 60},
         "mtpd_minutes": {b: 240, c: 240}, "data_loss_minutes": {b: 20, c: 5},
         "rpo_minutes": {b: 10, c: 10}},
        {"affected_ids": [a, b], "forecast": {b: {"rto_exceeded": False, "mtpd_exceeded": False},
                                             c: {"rto_exceeded": False, "mtpd_exceeded": False}}})
    codes = [d["code"] for d in result["differences"]]
    assert "affected_not_recovered" in codes  # a never recovered
    assert "recovered_not_in_simulation" in codes  # c was never in the simulation
    assert "rto_missed_in_drill" in codes  # b and c both missed RTO
    assert "drill_missed_rto_simulation_met" in codes  # forecast said b would hold
    assert "mtpd_exceeded_in_drill" in codes  # c exceeded MTPD
    assert "data_loss_exceeds_rpo" in codes  # b lost more than its RPO
    assert result["matches"] is False
    assert result["affected"] == 2 and result["recovered"] == 2 and result["simulated"] == 2


def test_compare_clean_when_everything_matches():
    server = str(uuid.uuid4())
    result = compare_drill_to_simulation(
        {"affected_ids": [server], "recovered_ids": [server],
         "recovery_minutes": {server: 30}, "rto_minutes": {server: 60},
         "mtpd_minutes": {server: 240}, "data_loss_minutes": {server: 5}, "rpo_minutes": {server: 10}},
        {"affected_ids": [server], "forecast": {server: {"rto_exceeded": False, "mtpd_exceeded": False}}})
    assert result == {"differences": [], "matches": True, "affected": 1, "recovered": 1, "simulated": 1}
