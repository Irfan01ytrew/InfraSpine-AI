import asyncio
from types import SimpleNamespace

from app.agent_studio.graph import _completed_action_answer, build_agent_graph


def test_malformed_model_action_is_not_exposed_as_user_text():
    async def planner(*_args, **_kwargs):
        return SimpleNamespace(reply='{"action":"tool","arguments":{"internal":')

    async def execute(*_args):
        raise AssertionError("Malformed action must never dispatch")

    agent = SimpleNamespace(
        name="IRFANIX", description="", instructions="", max_steps=1,
        tool_permissions=["my_work"], approval_tools=[], rag_enabled=False, delegate_agent_ids=[],
    )
    graph = build_agent_graph(agent, SimpleNamespace(chat=planner), execute, user_email="test@example.invalid")
    result = asyncio.run(graph.ainvoke({"user_input": "hello"}))
    assert result["failure_code"] == "invalid_model_action"
    assert "internal" not in result["answer"]


def test_agent_budget_stops_a_model_that_keeps_requesting_tools():
    calls = []

    async def planner(*_args, **_kwargs):
        return SimpleNamespace(reply='{"action":"tool","tool":"my_work","arguments":{}}')

    async def execute(name, *_args):
        calls.append(name)
        return {"count": 0, "tasks": []}

    agent = SimpleNamespace(
        name="IRFANIX", description="", instructions="", max_steps=1,
        tool_permissions=["my_work"], approval_tools=[], rag_enabled=False, delegate_agent_ids=[],
    )
    graph = build_agent_graph(agent, SimpleNamespace(chat=planner), execute, user_email="test@example.invalid")
    result = asyncio.run(graph.ainvoke({"user_input": "show my work"}))
    assert result["failure_code"] == "action_budget_exhausted"
    assert calls == ["my_work"]


def test_repeated_actions_do_not_dump_tool_payloads():
    assert "confidential" not in _completed_action_answer("project_file_read", {"contents": "confidential"})
    assert "Review agenda" in _completed_action_answer("my_work", {"tasks": [{"title": "Review agenda"}]})
