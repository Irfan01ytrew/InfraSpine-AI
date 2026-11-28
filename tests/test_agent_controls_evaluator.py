"""Tests for the Step AK per-agent controls evaluator (slice 1).

Pure functions only: no database, no network, no app stack beyond
``app.agent_controls``. Run with ``--noconftest`` in a pytest-only venv.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.agent_controls import (
    ActionRequest,
    AgentControl,
    check_delegation,
    evaluate,
    kill_switch,
    spend_status,
    validate_control,
)

AGENT = uuid4()
OWNER = uuid4()
DELEGATE = uuid4()
OTHER = uuid4()


def control(**overrides):
    base = dict(
        agent_id=AGENT,
        agent_kind="studio",
        owner_id=OWNER,
        status="active",
        data_scopes=frozenset({"knowledge:finance", "documents:dept-sales"}),
        permissions=frozenset({"documents.read", "knowledge.search"}),
        allowed_actions=frozenset({"knowledge_search", "document.metadata"}),
        spend_limit_usd=Decimal("10"),
        spend_used_usd=Decimal("2"),
        max_steps=6,
        delegate_to=frozenset({DELEGATE}),
        max_delegation_depth=1,
    )
    base.update(overrides)
    return AgentControl(**base)


def request(**overrides):
    base = dict(
        action="knowledge_search",
        scope="knowledge:finance",
        permission="knowledge.search",
        estimated_cost_usd=Decimal("0.50"),
        risk="low",
    )
    base.update(overrides)
    return ActionRequest(**base)


# --------------------------------------------------------------------- happy path

def test_allowed_request_is_granted_with_no_alerts():
    decision = evaluate(control(), request())
    assert decision.outcome == "allow"
    assert decision.reason == "granted"
    assert decision.alerts == ()
    assert decision.details["action"] == "knowledge_search"


def test_no_spend_limit_means_no_spend_alert():
    decision = evaluate(control(spend_limit_usd=None), request())
    assert decision.outcome == "allow"
    assert decision.alerts == ()


def test_no_permission_needed_when_request_has_none():
    decision = evaluate(control(), request(permission=None))
    assert decision.outcome == "allow"


# --------------------------------------------------------------------- liveness first

def test_killed_agent_is_denied_before_anything_else():
    dead = control(status="killed", killed_at=datetime.now(UTC), kill_reason="rogue loop")
    decision = evaluate(dead, request())
    assert (decision.outcome, decision.reason) == ("deny", "agent_killed")


def test_disabled_and_paused_agents_are_denied():
    assert evaluate(control(status="disabled"), request()).reason == "agent_disabled"
    assert evaluate(control(status="paused"), request()).reason == "agent_paused"


# --------------------------------------------------------------------- allow-list, scope, permission

def test_action_not_in_allow_list_is_denied():
    decision = evaluate(control(), request(action="public_web_research"))
    assert (decision.outcome, decision.reason) == ("deny", "action_not_allowed")


def test_scope_not_covered_is_denied():
    decision = evaluate(control(), request(scope="knowledge:hr"))
    assert (decision.outcome, decision.reason) == ("deny", "scope_not_covered")


def test_permission_missing_is_denied():
    decision = evaluate(control(), request(permission="finance.read"))
    assert (decision.outcome, decision.reason) == ("deny", "permission_missing")


def test_empty_allow_list_denies_everything():
    decision = evaluate(control(allowed_actions=frozenset()), request())
    assert (decision.outcome, decision.reason) == ("deny", "action_not_allowed")


# --------------------------------------------------------------------- spend

def test_spend_over_limit_is_denied_before_running():
    decision = evaluate(
        control(spend_used_usd=Decimal("9.80")),
        request(estimated_cost_usd=Decimal("0.50")),
    )
    assert (decision.outcome, decision.reason) == ("deny", "spend_limit_exceeded")
    assert decision.details["limit"] == "10"


def test_spend_exactly_at_limit_is_allowed_with_alert():
    decision = evaluate(
        control(spend_used_usd=Decimal("9.50")),
        request(estimated_cost_usd=Decimal("0.50")),
    )
    assert decision.outcome == "allow"
    assert decision.alerts == ("spend_near_limit",)


def test_spend_near_limit_is_allowed_with_alert():
    decision = evaluate(
        control(spend_used_usd=Decimal("7.90")),
        request(estimated_cost_usd=Decimal("0.50")),
    )
    assert decision.outcome == "allow"
    assert decision.alerts == ("spend_near_limit",)


def test_spend_well_under_limit_has_no_alert():
    decision = evaluate(control(), request())
    assert decision.outcome == "allow"
    assert decision.alerts == ()


def test_spend_status_states():
    assert spend_status(control(spend_limit_usd=None))["state"] == "ok"
    assert spend_status(control())["state"] == "ok"
    assert spend_status(control(spend_used_usd=Decimal("8")))["state"] == "near_limit"
    assert spend_status(control(spend_used_usd=Decimal("10")))["state"] == "at_limit"
    assert spend_status(control(spend_limit_usd=Decimal("0"), spend_used_usd=Decimal("0")))["state"] == "at_limit"
    status = spend_status(control(spend_used_usd=Decimal("2.50")))
    assert status["remaining"] == Decimal("7.50")
    assert status["percent_used"] == Decimal("0.25")


# --------------------------------------------------------------------- approval gates

def test_high_and_critical_risk_need_approval():
    assert evaluate(control(), request(risk="high")).outcome == "needs_approval"
    assert evaluate(control(), request(risk="high")).reason == "approval_required"
    assert evaluate(control(), request(risk="critical")).outcome == "needs_approval"


def test_low_and_medium_risk_do_not_need_approval():
    assert evaluate(control(), request(risk="medium")).outcome == "allow"


def test_sensitive_action_needs_approval_even_when_allowed():
    decision = evaluate(
        control(allowed_actions=frozenset({"document.share"}),
                permissions=frozenset({"documents.share"})),
        request(action="document.share", scope="documents:dept-sales",
                permission="documents.share", risk="low"),
    )
    assert (decision.outcome, decision.reason) == ("needs_approval", "sensitive_action")


def test_sensitive_action_still_needs_approval_when_spend_alert_fires():
    decision = evaluate(
        control(allowed_actions=frozenset({"document.share"}),
                permissions=frozenset({"documents.share"}),
                spend_used_usd=Decimal("8")),
        request(action="document.share", scope="documents:dept-sales",
                permission="documents.share", risk="low"),
    )
    assert decision.outcome == "needs_approval"
    assert decision.alerts == ("spend_near_limit",)


# --------------------------------------------------------------------- validation

@pytest.mark.parametrize("field,value", [
    ("agent_kind", "unknown"),
    ("status", "sleeping"),
    ("permissions", frozenset({"*"})),
    ("allowed_actions", frozenset({"*"})),
    ("permissions", frozenset({""})),
    ("spend_used_usd", Decimal("-1")),
    ("spend_limit_usd", Decimal("-1")),
    ("spend_limit_usd", Decimal("1")),  # used (2) > limit (1)
    ("alert_threshold", Decimal("0")),
    ("alert_threshold", Decimal("1.5")),
    ("max_steps", 0),
    ("max_steps", 9),
    ("max_delegation_depth", -1),
    ("delegate_to", frozenset({AGENT})),
])
def test_invalid_control_records_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_control(control(**{field: value}))


def test_killed_without_timestamp_is_rejected():
    with pytest.raises(ValueError):
        validate_control(control(status="killed", kill_reason="x"))


def test_non_killed_with_kill_details_is_rejected():
    with pytest.raises(ValueError):
        validate_control(control(killed_at=datetime.now(UTC)))


def test_invalid_requests_are_rejected():
    with pytest.raises(ValueError):
        evaluate(control(), request(risk="extreme"))
    with pytest.raises(ValueError):
        evaluate(control(), request(estimated_cost_usd=Decimal("-0.01")))
    with pytest.raises(ValueError):
        evaluate(control(), request(action=""))
    with pytest.raises(ValueError):
        evaluate(control(), request(scope=""))


# --------------------------------------------------------------------- kill-switch

def test_kill_switch_kills_and_audits():
    actor = uuid4()
    killed, event = kill_switch(control(), actor_id=actor, reason="  rogue   loop  ")
    assert killed.status == "killed"
    assert killed.kill_reason == "rogue loop"
    assert killed.killed_by == actor
    assert killed.killed_at is not None
    assert event.event == "agent.killed"
    assert event.agent_id == AGENT
    assert event.actor_id == actor
    assert event.reason == "rogue loop"
    assert event.before["status"] == "active"
    assert event.after["status"] == "killed"
    # The killed record denies everything from now on.
    assert evaluate(killed, request()).reason == "agent_killed"


def test_kill_switch_requires_a_reason():
    with pytest.raises(ValueError):
        kill_switch(control(), actor_id=uuid4(), reason="   ")


def test_repeat_kill_is_a_no_op_with_event():
    killed, _ = kill_switch(control(), actor_id=uuid4(), reason="first")
    same, event = kill_switch(killed, actor_id=uuid4(), reason="second attempt")
    assert same is killed
    assert event.event == "agent.kill_repeat"
    assert event.after["kill_reason"] == "first"


def test_kill_switch_never_revives():
    killed, _ = kill_switch(control(), actor_id=uuid4(), reason="done")
    with pytest.raises(ValueError):
        # A killed record without its kill details is invalid; kill_switch
        # itself never produces a live record from a killed one.
        validate_control(AgentControl(agent_id=AGENT, status="active",
                                      killed_at=killed.killed_at))


# --------------------------------------------------------------------- delegation

def test_delegation_allowed_within_depth():
    decision = check_delegation(control(), DELEGATE, depth=1)
    assert (decision.outcome, decision.reason) == ("allow", "granted")


def test_delegation_to_unlisted_agent_is_denied():
    decision = check_delegation(control(), OTHER, depth=1)
    assert (decision.outcome, decision.reason) == ("deny", "delegate_not_allowed")


def test_delegation_depth_exceeded_is_denied():
    decision = check_delegation(control(), DELEGATE, depth=2)
    assert (decision.outcome, decision.reason) == ("deny", "delegation_depth_exceeded")


def test_delegation_to_dead_agent_is_denied():
    decision = check_delegation(control(), DELEGATE, depth=1, delegate_alive=False)
    assert (decision.outcome, decision.reason) == ("deny", "delegate_not_alive")


def test_dead_agent_delegates_nothing():
    dead = control(status="killed", killed_at=datetime.now(UTC), kill_reason="x")
    assert check_delegation(dead, DELEGATE).reason == "agent_killed"
    assert check_delegation(control(status="paused"), DELEGATE).reason == "agent_paused"
    assert check_delegation(control(status="disabled"), DELEGATE).reason == "agent_disabled"


def test_delegation_depth_below_one_is_invalid():
    with pytest.raises(ValueError):
        check_delegation(control(), DELEGATE, depth=0)
