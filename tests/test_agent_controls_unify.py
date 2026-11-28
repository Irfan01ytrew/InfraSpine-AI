"""Step AK slice 5: unification adapters — legacy islands as control views.

Pure-function tests: no database, no network. The adapters must never
widen access: anything a legacy island does not express stays empty
(deny by default) and is reported in `notes`, and a call-window budget
is never mistaken for money.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.agent_controls.evaluator import evaluate, ActionRequest
from app.agent_controls.unify import (
    control_from_governance,
    control_from_studio,
    effective_control,
)


def _studio(**overrides):
    base = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        name="Research agent",
        tool_permissions=["knowledge_search", "document.metadata"],
        approval_tools=["document.share"],
        delegate_agent_ids=[],
        rag_enabled=True,
        max_steps=6,
        enabled=True,
        paused=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _governance(**overrides):
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        name="Security agent",
        tools=["scan", "report"],
        enabled=True,
        max_calls=20,
        window_seconds=3600,
        timeout_seconds=30,
        calls_used=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_studio_active_agent_maps_tools_and_steps():
    view = control_from_studio(_studio())
    assert view.control.status == "active"
    assert view.control.agent_kind == "studio"
    assert view.control.allowed_actions == {"knowledge_search", "document.metadata"}
    assert view.control.max_steps == 6
    assert view.control.spend_limit_usd is None
    assert view.control.spend_used_usd == Decimal("0")
    # Nothing the island does not express is guessed.
    assert view.control.permissions == frozenset()
    assert view.control.data_scopes == frozenset()
    assert any("rag_enabled" in note for note in view.notes)


def test_studio_paused_and_disabled_status():
    assert control_from_studio(_studio(paused=True)).control.status == "paused"
    assert control_from_studio(_studio(enabled=False)).control.status == "disabled"
    assert control_from_studio(_studio(enabled=False, paused=True)).control.status == "disabled"


def test_studio_self_delegation_and_bad_ids_dropped():
    agent = _studio()
    agent.delegate_agent_ids = [str(agent.id), "not-a-uuid", str(uuid.uuid4())]
    view = control_from_studio(agent)
    assert agent.id not in view.control.delegate_to
    assert len(view.control.delegate_to) == 1
    assert any("Self-delegation" in note for note in view.notes)
    assert any("could not be parsed" in note for note in view.notes)


def test_studio_max_steps_clamped_to_1_8():
    assert control_from_studio(_studio(max_steps=99)).control.max_steps == 8
    assert control_from_studio(_studio(max_steps=0)).control.max_steps == 1


def test_governance_maps_tools_and_never_money():
    view = control_from_governance(_governance())
    assert view.control.status == "active"
    assert view.control.agent_kind == "governance"
    assert view.control.allowed_actions == {"scan", "report"}
    assert view.control.spend_limit_usd is None
    assert view.control.spend_used_usd == Decimal("0")
    assert view.control.permissions == frozenset()
    assert view.control.data_scopes == frozenset()
    # The call-window budget is reported, never converted to money.
    assert any("5/20" in note and "3600" in note for note in view.notes)


def test_governance_disabled_agent():
    assert control_from_governance(_governance(enabled=False)).control.status == "disabled"


def test_legacy_views_evaluate_like_controls():
    view = control_from_studio(_studio())
    allowed = evaluate(view.control, ActionRequest(action="knowledge_search", scope="knowledge:rag"))
    # Empty data scopes deny by default — the view grants nothing new.
    assert allowed.outcome == "deny"
    assert allowed.reason == "scope_not_covered"
    listed = evaluate(
        view.control, ActionRequest(action="document.metadata", scope="knowledge:rag", permission=None))
    assert listed.outcome == "deny"
    assert listed.reason == "scope_not_covered"


def test_effective_control_record_wins_over_legacy():
    record_view = control_from_studio(_studio())
    legacy = control_from_governance(_governance())
    chosen = effective_control(record_view.control, legacy)
    assert chosen.control is record_view.control
    assert chosen.notes == ("Authoritative control record.",)
    assert effective_control(None, legacy) is legacy
    assert effective_control(None, None, None) is None
