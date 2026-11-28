"""Tests for Step AH slice 4 (pure part): app/resilience/plans.py.

Pure-function tests over synthetic snapshots and analyses — no database
rows, no live data, no secrets. Fake people, teams, systems, suppliers
and amounts only.
"""

import pytest

from app.resilience.impact import score_disruption
from app.resilience.plans import (
    APPROVAL_HANDLER,
    EXECUTE_HANDLER,
    SENSITIVE_APPROVAL_PERMISSION,
    STEP_APPROVAL_PERMISSION,
    build_plan,
    plan_fingerprint,
    plan_progress_from_steps,
    plan_spec,
    step_action_params,
    step_subject,
)
from app.workflows.definition import Definition, validate_graph


def _entity(entity_id, kind, name=None, **attrs):
    return {"id": entity_id, "kind": kind, "name": name or entity_id, "attributes": dict(attrs)}


def _rel(source_id, target_id, kind, confidence=1.0):
    return {
        "id": f"{source_id}->{target_id}:{kind}",
        "source_id": source_id,
        "target_id": target_id,
        "kind": kind,
        "confidence": confidence,
    }


def _snapshot():
    """Ayesha approves a pending batch, Bilal backs her up; two suppliers
    feed one process; the process records a manual workaround."""
    entities = [
        _entity("p-ayesha", "person", "Ayesha", skills=["approvals"], critical=True),
        _entity("p-bilal", "person", "Bilal", skills=["approvals"]),
        _entity("b-1", "invoice_batch", "Batch 1", amount=1500, currency="PKR",
                status="awaiting_approval", required_skills=["approvals"]),
        _entity("proc-1", "process", "Pay suppliers", critical=True,
                manual_workaround="Pay by bank transfer with the paper form."),
        _entity("s-1", "supplier", "Supplier One"),
        _entity("s-2", "supplier", "Supplier Two"),
    ]
    relationships = [
        _rel("p-ayesha", "b-1", "approves"),
        _rel("p-bilal", "p-ayesha", "backup_for"),
        _rel("s-1", "proc-1", "supplies"),
        _rel("s-2", "proc-1", "supplies"),
        _rel("proc-1", "b-1", "depends_on"),
    ]
    return {"entities": entities, "relationships": relationships}


def _analysis_for_supplier_failure():
    return score_disruption(
        _snapshot(),
        {"kind": "supplier_failure", "target_ids": ["s-1"], "duration_days": 3},
    )


def test_build_plan_picks_options_in_order():
    analysis = _analysis_for_supplier_failure()
    kinds = [option["kind"] for option in analysis["recovery_options"]]
    assert "switch_supplier" in kinds
    assert "manual_workaround" in kinds
    assert "escalate" in kinds
    switch_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "switch_supplier")
    escalate_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "escalate")
    plan = build_plan(analysis, [switch_rank, escalate_rank], plan_id="plan-test-1")
    assert plan["id"] == "plan-test-1"
    assert plan["severity"] == analysis["severity"]
    assert [step["id"] for step in plan["steps"]] == ["s1", "s2"]
    assert plan["steps"][0]["kind"] == "switch_supplier"
    assert plan["steps"][0]["requires_approval"] is True
    assert plan["steps"][0]["sensitive"] is True
    assert plan["steps"][0]["approval_permission"] == SENSITIVE_APPROVAL_PERMISSION
    assert plan["steps"][1]["kind"] == "escalate"
    assert plan["steps"][1]["requires_approval"] is False
    assert plan["steps"][1]["approval_permission"] is None
    assert plan["steps"][0]["covers"] == ["proc-1"]


def test_build_plan_backup_step_uses_workflow_manage():
    analysis = score_disruption(
        _snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"], "duration_days": 2},
    )
    backup_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "use_backup")
    plan = build_plan(analysis, [backup_rank])
    step = plan["steps"][0]
    assert step["kind"] == "use_backup"
    assert step["requires_approval"] is True
    assert step["sensitive"] is False
    assert step["approval_permission"] == STEP_APPROVAL_PERMISSION
    assert step["owner"] == "p-bilal"
    assert step["covers"] == ["b-1"]


def test_build_plan_rejects_bad_choices():
    analysis = _analysis_for_supplier_failure()
    with pytest.raises(ValueError, match="at least one"):
        build_plan(analysis, [])
    with pytest.raises(ValueError, match="chosen twice"):
        build_plan(analysis, [1, 1])
    with pytest.raises(ValueError, match="No recovery option with rank 99"):
        build_plan(analysis, [99])
    with pytest.raises(ValueError, match="integers"):
        build_plan(analysis, ["1"])
    with pytest.raises(ValueError, match="no recovery options"):
        build_plan({"recovery_options": []}, [1])
    with pytest.raises(ValueError, match="must be a dict"):
        build_plan(None, [1])


def test_plan_fingerprint_deterministic_and_sensitive_to_change():
    analysis = _analysis_for_supplier_failure()
    plan_a = build_plan(analysis, [1, 2], plan_id="plan-a")
    plan_b = build_plan(analysis, [1, 2], plan_id="plan-b")
    assert plan_fingerprint(plan_a) == plan_fingerprint(plan_b)
    plan_b["steps"][0]["action"] = "A different action."
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_b)


def test_plan_spec_is_a_valid_definition_with_approval_chain():
    analysis = _analysis_for_supplier_failure()
    switch_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "switch_supplier")
    escalate_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "escalate")
    plan = build_plan(analysis, [switch_rank, escalate_rank], plan_id="plan-spec-1")
    spec = plan_spec(plan)
    definition = Definition.model_validate(spec)
    assert validate_graph(definition) == []
    assert spec["input_form"] == [
        {"name": "plan_id", "label": "Recovery plan", "type": "text", "required": True}
    ]
    node_ids = [node.id for node in definition.nodes]
    assert node_ids == ["start", "approve-s1", "exec-s1", "exec-s2", "end"]
    approve = next(n for n in definition.nodes if n.id == "approve-s1")
    assert approve.type == "action"
    assert approve.config.handler == APPROVAL_HANDLER
    assert approve.config.auto is True
    assert approve.risk == "critical"  # sensitive step
    execute = next(n for n in definition.nodes if n.id == "exec-s1")
    assert execute.type == "action"
    assert execute.config.handler == EXECUTE_HANDLER
    assert execute.config.permission == "workflow.manage"
    assert execute.config.values["plan_id"] == "plan-spec-1"
    assert execute.config.values["step_id"] == "s1"
    # The escalate step needs no approval: no approve-s2 node.
    assert "approve-s2" not in node_ids
    edges = {(e.source, e.target) for e in definition.edges}
    assert edges == {
        ("start", "approve-s1"),
        ("approve-s1", "exec-s1"),
        ("exec-s1", "exec-s2"),
        ("exec-s2", "end"),
    }


def test_plan_spec_rejects_empty_plan():
    with pytest.raises(ValueError, match="at least one step"):
        plan_spec({"id": "plan-x", "steps": []})


def test_step_subject_and_params_are_exact_and_secret_free():
    analysis = _analysis_for_supplier_failure()
    plan = build_plan(analysis, [1], plan_id="plan-params")
    step = plan["steps"][0]
    assert step_subject("plan-params", "s1") == "resilience-plan:plan-params:s1"
    params = step_action_params(plan, step, run_id="00000000-0000-0000-0000-000000000001")
    assert params["plan_id"] == "plan-params"
    assert params["step_id"] == "s1"
    assert params["kind"] == step["kind"]
    assert params["run_id"] == "00000000-0000-0000-0000-000000000001"
    assert "amount" not in params
    assert "payee" not in params


def test_plan_progress_from_steps():
    analysis = _analysis_for_supplier_failure()
    switch_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "switch_supplier")
    escalate_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "escalate")
    plan = build_plan(analysis, [switch_rank, escalate_rank], plan_id="plan-progress")
    rows = [
        {"node_id": "approve-s1", "status": "completed", "acting_user_id": None, "verified_at": None},
        {"node_id": "exec-s1", "status": "completed", "acting_user_id": "user-1",
         "verified_at": "2026-11-28T01:00:00+00:00"},
    ]
    progress = plan_progress_from_steps(plan, rows)
    assert progress["plan_id"] == "plan-progress"
    assert progress["total_steps"] == 2
    assert progress["percent_complete"] == 50.0
    assert progress["next_step"] == "s2"
    assert progress["complete"] is False
    assert progress["steps"][0]["status"] == "done"
    assert progress["steps"][0]["acting_user_id"] == "user-1"
    assert progress["steps"][1]["status"] == "pending"
    # s2 has no approval node (escalate needs none), so it is not waiting.
    assert progress["steps"][1]["waiting_for_approval"] is False


def test_plan_progress_waiting_for_approval():
    analysis = score_disruption(
        _snapshot(),
        {"kind": "person_absence", "target_ids": ["p-ayesha"], "duration_days": 2},
    )
    backup_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "use_backup")
    escalate_rank = next(o["rank"] for o in analysis["recovery_options"] if o["kind"] == "escalate")
    plan = build_plan(analysis, [backup_rank, escalate_rank], plan_id="plan-wait")
    rows = [{"node_id": "approve-s1", "status": "waiting", "acting_user_id": None, "verified_at": None}]
    progress = plan_progress_from_steps(plan, rows)
    assert progress["steps"][0]["status"] == "pending"
    assert progress["steps"][0]["waiting_for_approval"] is True
    assert progress["percent_complete"] == 0.0


def test_plan_progress_all_done():
    analysis = _analysis_for_supplier_failure()
    plan = build_plan(analysis, [1], plan_id="plan-done")
    rows = [{"node_id": "exec-s1", "status": "completed", "acting_user_id": "user-1",
             "verified_at": "2026-11-28T01:00:00+00:00"}]
    progress = plan_progress_from_steps(plan, rows)
    assert progress["percent_complete"] == 100.0
    assert progress["complete"] is True
    assert progress["next_step"] is None
