"""Step N slice 4 — control-plane inventory, pure view tests (no DB).

The agent views follow Step AK's ``unify.py`` rules: a real control
record always wins, legacy islands leave unexpressed fields empty
(deny by default), and a call-window budget is never money.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.cost_intelligence.inventory import (
    _cap,
    ak_record_view,
    effective_agent_view,
    governance_agent_view,
    inventory_fingerprint,
    studio_agent_view,
)


def _studio(**overrides):
    base = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "name": "helper",
        "enabled": True,
        "paused": False,
        "max_steps": 6,
        "tool_permissions": ["rag.search", "docs.read"],
        "approval_tools": ["finance.pay"],
        "delegate_agent_ids": [],
        "rag_enabled": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _governance(**overrides):
    base = {
        "id": uuid.uuid4(),
        "owner_user_id": uuid.uuid4(),
        "name": "guard",
        "tools": ["approve"],
        "enabled": True,
        "max_calls": 20,
        "calls_used": 3,
        "window_seconds": 3600,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_studio_view_active_defaults():
    view = studio_agent_view(_studio())
    assert view["kind"] == "studio"
    assert view["status"] == "active"
    assert view["allowed_actions"] == ("docs.read", "rag.search")
    assert view["approval_tools"] == ("finance.pay",)
    assert "permissions" not in view  # Studio has no permission list: deny by default
    assert view["spend_limit_usd"] is None
    assert view["spend_used_usd"] == "0.0000"
    assert view["max_steps"] == 6
    assert view["rag_enabled"] is False
    assert any("deny by default" in note for note in view["notes"])


def test_studio_view_status_mapping():
    assert studio_agent_view(_studio(paused=True))["status"] == "paused"
    assert studio_agent_view(_studio(enabled=False))["status"] == "disabled"
    assert studio_agent_view(_studio(enabled=False, paused=True))["status"] == "disabled"


def test_studio_view_max_steps_clamped_with_note():
    assert studio_agent_view(_studio(max_steps=0))["max_steps"] == 1
    view = studio_agent_view(_studio(max_steps=99))
    assert view["max_steps"] == 8
    assert any("clamped to 8" in note for note in view["notes"])
    assert studio_agent_view(_studio(max_steps="not-a-number"))["max_steps"] == 6


def test_studio_view_delegates_parsed_self_dropped_bad_dropped():
    agent_id = uuid.uuid4()
    other = uuid.uuid4()
    view = studio_agent_view(_studio(
        id=agent_id, delegate_agent_ids=[str(other), str(agent_id), "nonsense", 42]
    ))
    assert view["delegate_to"] == [str(other)]
    assert any("could not be parsed" in note for note in view["notes"])
    assert any("Self-delegation was dropped" in note for note in view["notes"])


def test_studio_view_rag_note_grants_nothing():
    view = studio_agent_view(_studio(rag_enabled=True))
    assert view["rag_enabled"] is True
    assert any("grants no data scope" in note for note in view["notes"])


def test_governance_view_call_budget_is_not_money():
    view = governance_agent_view(_governance())
    assert view["kind"] == "governance"
    assert view["status"] == "active"
    assert view["call_budget"] == {"calls_used": 3, "max_calls": 20, "window_seconds": 3600}
    assert view["spend_limit_usd"] is None
    assert view["spend_used_usd"] == "0.0000"
    assert view["max_steps"] == 6
    assert any("is not money" in note for note in view["notes"])
    assert any("no step limit" in note for note in view["notes"])


def test_governance_view_disabled():
    assert governance_agent_view(_governance(enabled=False))["status"] == "disabled"


def test_ak_record_view_carries_record_fields():
    control = SimpleNamespace(
        agent_id=uuid.uuid4(),
        agent_kind="studio",
        owner_id=uuid.uuid4(),
        status="paused",
        permissions={"b.read", "a.write"},
        data_scopes={str(uuid.uuid4())},
        allowed_actions={"rag.search"},
        spend_limit_usd="12.5000",
        spend_used_usd="1.2500",
        max_steps=4,
        delegate_to={str(uuid.uuid4())},
    )
    view = ak_record_view(control)
    assert view["kind"] == "control-record:studio"
    assert view["status"] == "paused"
    assert view["permissions"] == ("a.write", "b.read")
    assert view["spend_limit_usd"] == "12.5000"
    assert view["spend_used_usd"] == "1.2500"
    assert view["max_steps"] == 4
    assert view["notes"] == ("Authoritative control record.",)


def test_effective_view_record_always_wins():
    record = {"agent_id": "r", "kind": "control-record:studio"}
    legacy = {"agent_id": "r", "kind": "studio"}
    assert effective_agent_view(record, legacy) is record
    assert effective_agent_view(None, legacy) is legacy
    assert effective_agent_view(None, None, legacy) is legacy
    assert effective_agent_view(None, None) is None


def test_fingerprint_deterministic_and_sensitive():
    sections = {"agents": [{"agent_id": "a", "kind": "studio"}], "policies": []}
    first = inventory_fingerprint(sections)
    assert inventory_fingerprint(sections) == first
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    changed = {"agents": [{"agent_id": "b", "kind": "studio"}], "policies": []}
    assert inventory_fingerprint(changed) != first


def test_hostile_text_is_data_only():
    view = studio_agent_view(_studio(name="'; DROP TABLE users; --"))
    assert view["name"] == "'; DROP TABLE users; --"
    assert view["status"] == "active"
    assert view["spend_limit_usd"] is None


def test_cap_refuses_truncation():
    with pytest.raises(ValueError, match="refusing to truncate"):
        _cap([{"x": 1}] * 5001, "agents")
    assert len(_cap([{"x": 1}], "agents")) == 1
