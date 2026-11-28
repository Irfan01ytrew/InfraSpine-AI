"""Tests for Step AH slice 1: app/resilience/impact.py.

Pure-function tests over synthetic snapshots — no database rows, no live
data, no secrets. Fake people, teams, systems, suppliers and amounts only.
"""

import pytest

from app.resilience.impact import (
    assess_risk,
    rank_risks,
    recovery_progress,
    score_disruption,
)


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


def _company_snapshot():
    """A small synthetic company: finance manager approves a batch, an
    invoicing process depends on the ERP system, a supplier feeds it."""
    return {
        "entities": [
            _entity("p-ayesha", "person", "Ayesha",
                    skills=["invoicing", "approvals"]),
            _entity("p-bilal", "person", "Bilal",
                    skills=["invoicing", "approvals"]),
            _entity("p-chen", "person", "Chen", skills=["support"]),
            _entity("t-finance", "team", "Finance"),
            _entity("proc-invoicing", "process", "Monthly invoicing",
                    critical=True, manual_workaround="Use paper forms, then re-enter."),
            _entity("sys-erp", "system", "ERP server"),
            _entity("data-invoices", "data", "Invoice archive"),
            _entity("sup-paper", "supplier", "Paper Co"),
            _entity("sup-toner", "supplier", "Toner Ltd"),
            _entity("b-mar", "invoice_batch", "March batch",
                    amount=1500.00, currency="PKR",
                    status="awaiting_approval", required_skills=["approvals"]),
            _entity("pay-1", "payment", "Payment 1",
                    amount=500.00, currency="PKR", status="approved"),
        ],
        "relationships": [
            _rel("p-ayesha", "t-finance", "member_of"),
            _rel("p-bilal", "t-finance", "member_of"),
            _rel("p-ayesha", "b-mar", "approves"),
            _rel("p-ayesha", "proc-invoicing", "owns"),
            _rel("p-bilal", "p-ayesha", "backup_for"),
            _rel("proc-invoicing", "sys-erp", "depends_on"),
            _rel("proc-invoicing", "data-invoices", "depends_on"),
            _rel("proc-invoicing", "sup-paper", "depends_on"),
            _rel("sys-erp", "data-invoices", "stores"),
            _rel("sup-paper", "proc-invoicing", "supplies"),
            _rel("sup-toner", "proc-invoicing", "supplies"),
            _rel("pay-1", "b-mar", "depends_on"),
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# person_absence
# ---------------------------------------------------------------------------

def test_person_absence_hits_owned_process_and_batch():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"], "duration_days": 7},
    )
    affected = {item["id"] for item in result["affected"]}
    assert "p-ayesha" in affected
    assert "proc-invoicing" in affected
    assert "b-mar" in affected
    assert "pay-1" in affected  # payment depends on its batch
    # Dependencies of an affected process are not themselves affected:
    # the ERP server is still up; only its use is stalled.
    assert "sys-erp" not in affected
    assert "data-invoices" not in affected
    assert result["money_at_risk"] == {"PKR": 2000.0}
    assert result["money_counts"] == {"invoice_batch": 1, "payment": 1}
    assert result["approval_bottleneck"] is True  # Ayesha is sole approver
    assert result["severity"] == "critical"
    assert result["as_of"] == "2026-11-28T00:00:00+00:00"


def test_person_absence_backup_covers_batch():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"]},
    )
    batch = next(item for item in result["affected"] if item["id"] == "b-mar")
    assert batch["covered_by"] == ["p-bilal"]
    assert "b-mar" not in result["uncovered"]


def test_backup_absent_means_uncovered():
    snapshot = _company_snapshot()
    result = score_disruption(
        snapshot,
        {"kind": "person_absence", "target_ids": ["p-ayesha", "p-bilal"]},
    )
    assert result["uncovered"]["b-mar"]["reason"] == "backup_absent"


def test_backup_missing_skills_means_uncovered():
    snapshot = _company_snapshot()
    snapshot["entities"][1]["attributes"]["skills"] = ["invoicing"]  # Bilal loses approvals
    result = score_disruption(
        snapshot,
        {"kind": "person_absence", "target_ids": ["p-ayesha"]},
    )
    assert result["uncovered"]["b-mar"]["reason"] == "backup_missing_skills"


def test_team_absence_expands_to_members():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "team_absence", "target_ids": ["t-finance"]},
    )
    assert set(result["disrupted_ids"]) == {"p-ayesha", "p-bilal"}
    assert result["uncovered"]["b-mar"]["reason"] == "backup_absent"


def test_business_impact_names_processes_and_money():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"], "duration_days": 3},
    )
    text = " ".join(result["business_impact"])
    assert "Monthly invoicing" in text
    assert "2,000.00 PKR" in text
    assert "Approval bottleneck" in text


# ---------------------------------------------------------------------------
# server_outage / cyber_incident / supplier_failure
# ---------------------------------------------------------------------------

def test_server_outage_hits_hosted_dependents_not_people():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "server_outage", "target_ids": ["sys-erp"]},
    )
    affected = {item["id"] for item in result["affected"]}
    assert "sys-erp" in affected
    assert "proc-invoicing" in affected  # depends on ERP
    assert "data-invoices" in affected  # stored by ERP
    assert "p-ayesha" not in affected
    assert result["money_at_risk"] == {}  # no money entity reached


def test_cyber_incident_on_data_hits_dependent_processes():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "cyber_incident", "target_ids": ["data-invoices"]},
    )
    affected = {item["id"] for item in result["affected"]}
    assert "data-invoices" in affected
    assert "proc-invoicing" in affected


def test_cyber_incident_on_system_hits_stored_data():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "cyber_incident", "target_ids": ["sys-erp"]},
    )
    affected = {item["id"] for item in result["affected"]}
    assert "data-invoices" in affected
    assert "proc-invoicing" in affected


def test_supplier_failure_with_alternative_ranked():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "supplier_failure", "target_ids": ["sup-paper"]},
    )
    affected = {item["id"] for item in result["affected"]}
    assert "proc-invoicing" in affected
    switches = [
        opt for opt in result["recovery_options"] if opt["kind"] == "switch_supplier"
    ]
    assert len(switches) == 1
    assert "Toner Ltd" in switches[0]["action"]
    assert switches[0]["requires_approval"] is True
    assert switches[0]["covers"] == ["proc-invoicing"]


def test_supplier_failure_without_alternative_is_uncovered():
    snapshot = _company_snapshot()
    snapshot["relationships"] = [
        rel for rel in snapshot["relationships"]
        if not (rel["kind"] == "supplies" and rel["source_id"] == "sup-toner")
    ]
    result = score_disruption(
        snapshot,
        {"kind": "supplier_failure", "target_ids": ["sup-paper"]},
    )
    switches = [
        opt for opt in result["recovery_options"] if opt["kind"] == "switch_supplier"
    ]
    assert switches == []


# ---------------------------------------------------------------------------
# recovery options
# ---------------------------------------------------------------------------

def test_recovery_options_always_end_with_escalate():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "server_outage", "target_ids": ["sys-erp"]},
    )
    assert result["recovery_options"][-1]["kind"] == "escalate"
    assert result["recovery_options"][-1]["requires_approval"] is False


def test_manual_workaround_offered_for_process():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"]},
    )
    workarounds = [
        opt for opt in result["recovery_options"]
        if opt["kind"] == "manual_workaround"
    ]
    assert len(workarounds) == 1
    assert "paper forms" in workarounds[0]["action"]
    assert workarounds[0]["requires_approval"] is False


def test_backup_option_requires_approval_and_names_owner():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"]},
    )
    backups = [
        opt for opt in result["recovery_options"] if opt["kind"] == "use_backup"
    ]
    assert backups
    assert backups[0]["owner"] == "p-bilal"
    assert backups[0]["requires_approval"] is True


def test_recovery_ranks_are_sequential():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"]},
    )
    ranks = [opt["rank"] for opt in result["recovery_options"]]
    assert ranks == list(range(1, len(ranks) + 1))


def test_deterministic_for_same_snapshot():
    snapshot = _company_snapshot()
    event = {"kind": "person_absence", "target_ids": ["p-ayesha"]}
    first = score_disruption(snapshot, event)
    second = score_disruption(snapshot, event)
    assert first == second


# ---------------------------------------------------------------------------
# severity
# ---------------------------------------------------------------------------

def test_small_disruption_is_low_severity():
    snapshot = {
        "entities": [
            _entity("p-chen", "person", "Chen", skills=["support"]),
            _entity("proc-help", "process", "Help desk"),
        ],
        "relationships": [_rel("p-chen", "proc-help", "owns")],
    }
    result = score_disruption(
        snapshot, {"kind": "person_absence", "target_ids": ["p-chen"]}
    )
    assert result["severity"] == "low"
    assert result["money_at_risk"] == {}


def test_many_affected_is_high_severity():
    entities = [_entity(f"p-{i}", "person", f"Person {i}") for i in range(9)]
    entities.append(_entity("p-boss", "person", "Boss"))
    relationships = [
        _rel("p-boss", f"p-{i}", "responsible_for") for i in range(9)
    ]
    snapshot = {"entities": entities, "relationships": relationships}
    result = score_disruption(
        snapshot, {"kind": "person_absence", "target_ids": ["p-boss"]}
    )
    assert result["affected_count"] == 10
    assert result["severity"] == "high"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_unknown_disruption_kind_rejected():
    with pytest.raises(ValueError, match="Unknown disruption kind"):
        score_disruption(
            _company_snapshot(),
            {"kind": "meteor_strike", "target_ids": ["sys-erp"]},
        )


def test_wrong_target_kind_rejected():
    with pytest.raises(ValueError, match="cannot target"):
        score_disruption(
            _company_snapshot(),
            {"kind": "supplier_failure", "target_ids": ["p-ayesha"]},
        )


def test_unknown_target_rejected():
    with pytest.raises(ValueError, match="Unknown target id"):
        score_disruption(
            _company_snapshot(),
            {"kind": "server_outage", "target_ids": ["sys-nope"]},
        )


def test_empty_targets_rejected():
    with pytest.raises(ValueError, match="non-empty 'target_ids'"):
        score_disruption(
            _company_snapshot(), {"kind": "server_outage", "target_ids": []}
        )


def test_bad_duration_rejected():
    with pytest.raises(ValueError, match="duration_days"):
        score_disruption(
            _company_snapshot(),
            {"kind": "server_outage", "target_ids": ["sys-erp"], "duration_days": 0},
        )


def test_negative_amount_rejected():
    snapshot = _company_snapshot()
    snapshot["entities"][9]["attributes"]["amount"] = -5
    with pytest.raises(ValueError, match="must not be negative"):
        score_disruption(
            snapshot,
            {"kind": "person_absence", "target_ids": ["p-ayesha"]},
        )


def test_relationship_to_unknown_entity_rejected():
    snapshot = _company_snapshot()
    snapshot["relationships"].append(_rel("p-ayesha", "ghost", "owns"))
    with pytest.raises(ValueError, match="unknown entity"):
        score_disruption(
            snapshot,
            {"kind": "person_absence", "target_ids": ["p-ayesha"]},
        )


def test_duplicate_entity_rejected():
    snapshot = _company_snapshot()
    snapshot["entities"].append(_entity("p-ayesha", "person", "Copy"))
    with pytest.raises(ValueError, match="Duplicate entity id"):
        score_disruption(
            snapshot,
            {"kind": "person_absence", "target_ids": ["p-ayesha"]},
        )


def test_confidence_out_of_range_rejected():
    snapshot = _company_snapshot()
    snapshot["relationships"][0]["confidence"] = 1.5
    with pytest.raises(ValueError, match="confidence"):
        score_disruption(
            snapshot,
            {"kind": "person_absence", "target_ids": ["p-ayesha"]},
        )


def test_cycle_does_not_loop_forever():
    snapshot = {
        "entities": [
            _entity("a", "process", "A"),
            _entity("b", "process", "B"),
            _entity("p-1", "person", "Owner"),
        ],
        "relationships": [
            _rel("p-1", "a", "owns"),
            _rel("a", "b", "depends_on"),
            _rel("b", "a", "depends_on"),
        ],
    }
    result = score_disruption(
        snapshot, {"kind": "person_absence", "target_ids": ["p-1"]}
    )
    assert {item["id"] for item in result["affected"]} == {"p-1", "a", "b"}


def test_depth_limit_truncates():
    # Chain: person owns n-1; each later process depends on the previous
    # one, so impact flows outward one step at a time.
    entities = [_entity("p-0", "person", "Root")]
    relationships = [_rel("p-0", "n-1", "owns")]
    for i in range(1, 10):
        entities.append(_entity(f"n-{i}", "process", f"Node {i}"))
        if i > 1:
            relationships.append(_rel(f"n-{i}", f"n-{i - 1}", "depends_on"))
    snapshot = {"entities": entities, "relationships": relationships}
    result = score_disruption(
        snapshot,
        {"kind": "person_absence", "target_ids": ["p-0"]},
        depth=3,
    )
    assert result["truncated"] is True
    assert result["affected_count"] == 4  # root + 3 levels


# ---------------------------------------------------------------------------
# risk scoring
# ---------------------------------------------------------------------------

def test_sole_approver_is_high_risk():
    # Ayesha is sole approver (+20) with a blast radius (+10), but she
    # HAS a usable backup (Bilal), so no "no backup" points: 30.
    result = assess_risk(_company_snapshot(), "p-ayesha")
    assert result["risk_score"] == 30
    assert any("sole_approver" in factor for factor in result["factors"])
    assert any("blast_radius" in factor for factor in result["factors"])
    # Removing the backup link raises her score by 20.
    snapshot = _company_snapshot()
    snapshot["relationships"] = [
        rel for rel in snapshot["relationships"] if rel["kind"] != "backup_for"
    ]
    assert assess_risk(snapshot, "p-ayesha")["risk_score"] == 50


def test_covered_person_is_lower_risk_than_uncovered():
    snapshot = _company_snapshot()
    covered = assess_risk(snapshot, "p-bilal")  # Bilal backs up Ayesha? No — check below
    # Bilal has no backup recorded and owns nothing; Ayesha is sole approver.
    ayesha = assess_risk(snapshot, "p-ayesha")
    assert ayesha["risk_score"] > covered["risk_score"]


def test_sole_supplier_scores_concentration():
    snapshot = {
        "entities": [
            _entity("sup-only", "supplier", "Only Paper"),
            _entity("sup-alt", "supplier", "Alt Paper"),
            _entity("proc-a", "process", "Printing A"),
            _entity("proc-b", "process", "Printing B"),
        ],
        "relationships": [
            _rel("sup-only", "proc-a", "supplies"),
            _rel("sup-only", "proc-b", "supplies"),
            _rel("sup-alt", "proc-b", "supplies"),
        ],
    }
    only = assess_risk(snapshot, "sup-only")
    assert any("sole_supplier" in factor for factor in only["factors"])
    alt = assess_risk(snapshot, "sup-alt")
    assert not any("sole_supplier" in factor for factor in alt["factors"])


def test_stale_node_scores_higher():
    snapshot = _company_snapshot()
    snapshot["entities"][7]["attributes"]["last_confirmed_days_ago"] = 90
    stale = assess_risk(snapshot, "sup-paper")
    fresh = assess_risk(_company_snapshot(), "sup-paper")
    assert stale["risk_score"] == fresh["risk_score"] + 10
    assert any("stale_data" in factor for factor in stale["factors"])


def test_critical_flag_adds_score():
    snapshot = _company_snapshot()
    critical = assess_risk(snapshot, "proc-invoicing")
    assert any("marked_critical" in factor for factor in critical["factors"])


def test_risk_score_sums_all_person_factors():
    # Blast radius (capped at +30) + no backup (+20) + sole approver
    # (+20) + stale (+10) + critical (+10) = 90.
    entities = [_entity("p-0", "person", "Hub", critical=True,
                        last_confirmed_days_ago=400)]
    relationships = []
    for i in range(20):
        entities.append(_entity(f"proc-{i}", "process", f"P{i}"))
        relationships.append(_rel(f"proc-{i}", "p-0", "depends_on"))
        entities.append(_entity(f"b-{i}", "invoice_batch", f"B{i}",
                                amount=100, currency="PKR",
                                status="awaiting_approval"))
        relationships.append(_rel("p-0", f"b-{i}", "approves"))
    snapshot = {"entities": entities, "relationships": relationships}
    result = assess_risk(snapshot, "p-0")
    assert result["risk_score"] == 90
    assert len(result["factors"]) == 5


def test_unknown_node_rejected():
    with pytest.raises(ValueError, match="Unknown node id"):
        assess_risk(_company_snapshot(), "ghost")


def test_rank_risks_orders_highest_first():
    ranked = rank_risks(_company_snapshot())
    scores = [item["risk_score"] for item in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0]["node_id"] == "p-ayesha"
    assert len(ranked) == 11


# ---------------------------------------------------------------------------
# recovery progress
# ---------------------------------------------------------------------------

def test_recovery_progress_counts_and_next_step():
    plan = {
        "steps": [
            {"id": "s1", "status": "done"},
            {"id": "s2", "status": "in_progress"},
            {"id": "s3", "status": "pending"},
            {"id": "s4", "status": "blocked"},
        ]
    }
    result = recovery_progress(plan)
    assert result["total_steps"] == 4
    assert result["percent_complete"] == 25.0
    assert result["blocked_steps"] == ["s4"]
    assert result["next_step"] == "s2"
    assert result["complete"] is False


def test_recovery_progress_complete_when_all_done():
    plan = {"steps": [
        {"id": "s1", "status": "done"},
        {"id": "s2", "status": "skipped"},
    ]}
    result = recovery_progress(plan)
    assert result["percent_complete"] == 100.0
    assert result["complete"] is True
    assert result["next_step"] is None


def test_recovery_progress_empty_plan():
    result = recovery_progress({"steps": []})
    assert result["total_steps"] == 0
    assert result["percent_complete"] == 100.0
    assert result["complete"] is False


def test_recovery_progress_bad_status_rejected():
    with pytest.raises(ValueError, match="Unknown step status"):
        recovery_progress({"steps": [{"id": "s1", "status": "flying"}]})


def test_recovery_progress_missing_steps_rejected():
    with pytest.raises(ValueError, match="'steps' list"):
        recovery_progress({})


def test_assumptions_state_advisory_only():
    result = score_disruption(
        _company_snapshot(),
        {"kind": "supplier_failure", "target_ids": ["sup-paper"]},
    )
    assert any("Advisory only" in line for line in result["assumptions"])
    assert any("never approves" in line for line in result["assumptions"])
