"""Step AM slice 4 — tests for app/foundry/deploy.py.

Pure-function tests run with `python -m pytest tests/test_foundry_deploy.py
--noconftest`. The one ActionSpec test imports the real platform approvals
module (no database involved) to prove the deploy hash is byte-identical to
the hash the approval the user sees.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.foundry.deploy import (
    DEPLOY_ACTION_TYPE,
    DEPLOY_PERMISSION,
    approve_deploy_record,
    deploy_action_canonical,
    deploy_action_hash,
    deploy_complete,
    mark_deploying,
    may_request_approval,
    plan_deploy,
    record_health,
    request_deploy_record,
    rollback,
    validate_approval_for_deploy,
)
from app.foundry.generate import generate_app
from app.foundry.preview import (
    SandboxVerification,
    TestResult,
    apply_gate,
    build_preview,
    decide_gate,
    evaluate_test_results,
    required_test_ids,
)
from app.foundry.spec import validate_spec

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
SCOPE = str(uuid.uuid4())
SLUG = "leave-tracker"
BUILD_FP = "a" * 64
TARGET = "codespace-demo"


def _valid_spec():
    return {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [
            {"name": "Requests", "path": "/requests", "endpoints": ["list_requests"]},
            {"name": "New request", "path": "/requests/new", "endpoints": ["create_request"]},
        ],
        "endpoints": [
            {"name": "list_requests", "method": "GET", "path": "/api/requests",
             "table": "leave_requests"},
            {"name": "create_request", "method": "POST", "path": "/api/requests",
             "table": "leave_requests"},
        ],
        "tables": [
            {"name": "leave_requests",
             "columns": [{"name": "id", "type": "uuid"},
                         {"name": "status", "type": "string"}]},
        ],
    }


def _ready_gate():
    """A (preview, test_decision, sandbox) triple whose gate is preview_ready."""
    spec = _valid_spec()
    validation = validate_spec(spec)
    assert validation.valid
    app = generate_app(spec, validation)
    preview = apply_gate(
        build_preview(app, spec, validation, health="passed"),
        decide_gate(
            build_preview(app, spec, validation, health="passed"),
            evaluate_test_results(
                required_test_ids(spec),
                [TestResult(name=t, passed=True) for t in required_test_ids(spec)]),
            SandboxVerification(passed=True, tables_verified=("leave_requests",),
                                rows_inserted=1)))
    # build_preview starts blocked; apply the passing decision to get preview_ready.
    test_decision = evaluate_test_results(
        required_test_ids(spec),
        [TestResult(name=t, passed=True) for t in required_test_ids(spec)])
    sandbox = SandboxVerification(passed=True, tables_verified=("leave_requests",),
                                  rows_inserted=1)
    ready_preview = build_preview(app, spec, validation, health="passed")
    return ready_preview, test_decision, sandbox


def _valid_check(**overrides):
    kwargs = {
        "scope_id": SCOPE,
        "slug": SLUG,
        "build_fingerprint": BUILD_FP,
        "target": TARGET,
        "approval_status": "approved",
        "approval_action_hash": deploy_action_hash(SCOPE, SLUG, BUILD_FP, TARGET),
        "requested_by": "user-requester",
        "decided_by": "user-approver",
        "expires_at": NOW + timedelta(hours=1),
        "now": NOW,
    }
    kwargs.update(overrides)
    return validate_approval_for_deploy(**kwargs)


def _approved_record(previous="b" * 64):
    plan = plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target=TARGET,
                       previous_build_fingerprint=previous,
                       allowed_targets=(TARGET,))
    record = request_deploy_record(plan, approval_id="approval-1")
    record = approve_deploy_record(record, _valid_check())
    return mark_deploying(record)


# ---------------------------------------------------------------------------
# Deploy action hash — bound to the exact build
# ---------------------------------------------------------------------------

def test_deploy_action_hash_matches_platform_action_spec():
    from app.platform.approvals import ActionSpec

    scope = uuid.uuid4()
    expected = ActionSpec(
        DEPLOY_ACTION_TYPE, scope,
        {"slug": SLUG, "build_fingerprint": BUILD_FP, "target": TARGET},
        subject=SLUG).hash()
    assert deploy_action_hash(str(scope), SLUG, BUILD_FP, TARGET) == expected


def test_deploy_action_canonical_shape():
    canonical = deploy_action_canonical(SCOPE, SLUG, BUILD_FP, TARGET)
    assert canonical["type"] == DEPLOY_ACTION_TYPE
    assert canonical["subject"] == SLUG
    assert canonical["amount"] is None
    assert canonical["currency"] is None
    assert canonical["params"] == {"slug": SLUG, "build_fingerprint": BUILD_FP,
                                   "target": TARGET}
    assert DEPLOY_PERMISSION == "foundry.deploy"


def test_hash_changes_when_any_bound_value_changes():
    base = deploy_action_hash(SCOPE, SLUG, BUILD_FP, TARGET)
    assert deploy_action_hash(str(uuid.uuid4()), SLUG, BUILD_FP, TARGET) != base
    assert deploy_action_hash(SCOPE, "other-app", BUILD_FP, TARGET) != base
    assert deploy_action_hash(SCOPE, SLUG, "c" * 64, TARGET) != base
    assert deploy_action_hash(SCOPE, SLUG, BUILD_FP, "other-target") != base
    assert deploy_action_hash(SCOPE, SLUG, BUILD_FP, TARGET) == base  # deterministic


# ---------------------------------------------------------------------------
# Gate: approval only for a preview_ready build
# ---------------------------------------------------------------------------

def test_may_request_approval_when_everything_passes():
    preview, test_decision, sandbox = _ready_gate()
    decision = may_request_approval(preview, test_decision, sandbox)
    assert decision.gate == "preview_ready"
    assert decision.approval_offered is True


def test_may_request_approval_blocked_when_health_pending():
    spec = _valid_spec()
    validation = validate_spec(spec)
    app = generate_app(spec, validation)
    preview = build_preview(app, spec, validation, health="pending")
    test_decision = evaluate_test_results(
        required_test_ids(spec),
        [TestResult(name=t, passed=True) for t in required_test_ids(spec)])
    decision = may_request_approval(preview, test_decision,
                                    SandboxVerification(passed=True))
    assert decision.gate == "blocked"
    assert any("health check" in r for r in decision.reasons)


def test_may_request_approval_blocked_when_a_test_failed():
    preview, _, sandbox = _ready_gate()
    spec = _valid_spec()
    failed = [TestResult(name=t, passed=True) for t in required_test_ids(spec)]
    failed[0] = TestResult(name=failed[0].name, passed=False, detail="boom")
    decision = may_request_approval(preview, evaluate_test_results(required_test_ids(spec), failed),
                                    sandbox)
    assert decision.gate == "blocked"


def test_may_request_approval_blocked_without_sandbox_verification():
    preview, test_decision, _ = _ready_gate()
    decision = may_request_approval(preview, test_decision, None)
    assert decision.gate == "blocked"
    assert "no sandbox verification ran" in decision.reasons


# ---------------------------------------------------------------------------
# Approval validation
# ---------------------------------------------------------------------------

def test_valid_approval_check_passes():
    check = _valid_check()
    assert check.valid is True
    assert check.reasons == ()


def test_pending_approval_is_not_valid():
    check = _valid_check(approval_status="pending")
    assert check.valid is False
    assert any(r.startswith("approval_not_approved") for r in check.reasons)


def test_rejected_approval_is_not_valid():
    check = _valid_check(approval_status="rejected")
    assert check.valid is False


def test_expired_approval_is_not_valid():
    check = _valid_check(expires_at=NOW - timedelta(seconds=1))
    assert check.valid is False
    assert "approval_expired" in check.reasons


def test_hash_mismatch_when_build_changed_after_approval():
    check = _valid_check(build_fingerprint="d" * 64)
    assert check.valid is False
    assert "approval_hash_mismatch" in check.reasons


def test_hash_mismatch_when_target_changed_after_approval():
    check = _valid_check(target="other-target")
    assert check.valid is False
    assert "approval_hash_mismatch" in check.reasons


def test_already_used_approval_is_not_valid():
    check = _valid_check(already_consumed=True)
    assert check.valid is False
    assert "approval_already_used" in check.reasons


def test_consumed_status_is_not_valid():
    check = _valid_check(approval_status="consumed")
    assert check.valid is False
    assert "approval_already_used" in check.reasons


def test_requester_cannot_be_the_approver():
    check = _valid_check(decided_by="user-requester")
    assert check.valid is False
    assert "separation_of_duties" in check.reasons


def test_approved_without_a_decider_is_not_valid():
    check = _valid_check(decided_by=None)
    assert check.valid is False
    assert "approval_missing_decider" in check.reasons


# ---------------------------------------------------------------------------
# Deploy plan: target is explicit, never invented
# ---------------------------------------------------------------------------

def test_plan_deploy_refuses_without_a_configured_target():
    with pytest.raises(ValueError, match="no_deploy_target_configured"):
        plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target=TARGET)


def test_plan_deploy_refuses_a_target_outside_the_allow_list():
    with pytest.raises(ValueError, match="deploy_target_not_allowed"):
        plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target="prod",
                    allowed_targets=(TARGET,))


def test_plan_deploy_requires_a_build_fingerprint():
    with pytest.raises(ValueError, match="build_fingerprint is required"):
        plan_deploy(slug=SLUG, build_fingerprint="", target=TARGET,
                    allowed_targets=(TARGET,))


def test_plan_deploy_is_approval_gated_with_steps():
    plan = plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target=TARGET,
                       previous_build_fingerprint="b" * 64,
                       allowed_targets=(TARGET,))
    assert plan.requires_approval is True
    assert plan.previous_build_fingerprint == "b" * 64
    assert len(plan.steps) == 4
    assert any("health check" in s for s in plan.steps)


# ---------------------------------------------------------------------------
# Deploy record lifecycle
# ---------------------------------------------------------------------------

def test_happy_path_deploy_completes_only_after_health_passes():
    record = _approved_record()
    assert record.status == "deploying"
    assert deploy_complete(record) is False
    record, rollback_record = record_health(record, passed=True)
    assert rollback_record is None
    assert record.status == "deployed"
    assert record.health == "passed"
    assert deploy_complete(record) is True


def test_failed_health_check_rolls_back_automatically():
    record = _approved_record(previous="b" * 64)
    record, rollback_record = record_health(record, passed=False,
                                            reason="health endpoint 500")
    assert record.status == "rolled_back"
    assert record.health == "failed"
    assert record.rollback_to == "b" * 64
    assert rollback_record is not None
    assert rollback_record.restored_build_fingerprint == "b" * 64
    assert rollback_record.failed_build_fingerprint == BUILD_FP
    assert deploy_complete(record) is False


def test_approve_requires_a_valid_check():
    plan = plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target=TARGET,
                       allowed_targets=(TARGET,))
    record = request_deploy_record(plan, approval_id="approval-1")
    with pytest.raises(ValueError, match="cannot approve deploy"):
        approve_deploy_record(record, _valid_check(approval_status="pending"))


def test_transitions_refuse_wrong_states():
    plan = plan_deploy(slug=SLUG, build_fingerprint=BUILD_FP, target=TARGET,
                       allowed_targets=(TARGET,))
    requested = request_deploy_record(plan, approval_id="approval-1")
    with pytest.raises(ValueError, match="cannot start a deploy"):
        mark_deploying(requested)
    with pytest.raises(ValueError, match="cannot record health"):
        record_health(requested, passed=True)
    with pytest.raises(ValueError, match="cannot roll back"):
        rollback(requested, "nope")
    approved = approve_deploy_record(requested, _valid_check())
    with pytest.raises(ValueError, match="cannot approve a deploy"):
        approve_deploy_record(approved, _valid_check())


def test_manual_rollback_restores_previous_build():
    record = _approved_record(previous="b" * 64)
    record, _ = record_health(record, passed=True)
    record, rollback_record = rollback(record, "owner asked to revert")
    assert record.status == "rolled_back"
    assert record.rollback_to == "b" * 64
    assert rollback_record.reason == "owner asked to revert"
    assert deploy_complete(record) is False
    with pytest.raises(ValueError, match="cannot roll back"):
        rollback(record, "again")


def test_failed_deploy_cannot_be_redeployed_from_the_same_record():
    record = _approved_record()
    record, _ = record_health(record, passed=False)
    with pytest.raises(ValueError, match="cannot start a deploy"):
        mark_deploying(record)
