"""Isolated policy checks for the agent profile capability allowlist."""

from types import SimpleNamespace

import pytest

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.services.agents import _validate_config, _validate_run_profile


def test_agent_profile_configuration_is_normalized_and_tool_order_is_stable():
    name, tools = _validate_config(
        name="  Market   Research  ",
        agent_type="market_research",
        knowledge_scope="department_only",
        tool_policy=["public_web_research", "knowledge_search"],
    )

    assert name == "Market Research"
    assert tools == ["knowledge_search", "public_web_research"]


def test_agent_profile_rejects_duplicate_capabilities():
    with pytest.raises(InvalidInputError, match="cannot be repeated"):
        _validate_config(
            name="Research",
            agent_type="knowledge_research",
            knowledge_scope="requester_acl",
            tool_policy=["knowledge_search", "knowledge_search"],
        )


def test_agent_profile_rejects_capabilities_outside_its_agent_type():
    with pytest.raises(InvalidInputError, match="not registered for this agent type"):
        _validate_config(
            name="Training",
            agent_type="employee_training",
            knowledge_scope="requester_acl",
            tool_policy=["public_web_research"],
        )


def test_agent_profile_rejects_unbounded_knowledge_scope():
    with pytest.raises(InvalidInputError, match="knowledge scope is not supported"):
        _validate_config(
            name="Research",
            agent_type="knowledge_research",
            knowledge_scope="all_company_documents",
            tool_policy=["knowledge_search"],
        )


def test_agent_run_requires_active_profile_and_registered_profile_capability():
    profile = SimpleNamespace(
        enabled=True,
        agent_type="knowledge_research",
        tool_policy=["knowledge_search"],
    )
    _validate_run_profile(profile, "knowledge_search")
    with pytest.raises(PermissionDeniedError, match="not enabled"):
        _validate_run_profile(profile, "public_web_research")
    profile.enabled = False
    with pytest.raises(NotFoundError, match="Agent profile not found"):
        _validate_run_profile(profile, "knowledge_search")


def test_agent_run_rejects_tool_tampering_across_agent_types():
    profile = SimpleNamespace(
        enabled=True,
        agent_type="employee_training",
        tool_policy=["public_web_research"],
    )
    with pytest.raises(PermissionDeniedError, match="not enabled"):
        _validate_run_profile(profile, "public_web_research")
