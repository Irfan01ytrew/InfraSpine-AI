"""Project workspace tools are seeded only for the authenticated administrator Master Agent."""

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login


def test_admin_master_gets_approved_project_tools_once_and_employee_does_not(client):
    from app.agent_studio.service import _MASTER_PROJECT_APPROVALS, _MASTER_PROJECT_TOOLS

    login(client, ADMIN_EMAIL)
    response = client.get("/api/agent-studio/agents")
    assert response.status_code == 200, response.text
    master = next(agent for agent in response.json()["agents"] if agent["name"] == "Master Agent")
    assert set(_MASTER_PROJECT_TOOLS) <= set(master["tool_permissions"])
    assert set(_MASTER_PROJECT_APPROVALS) <= set(master["approval_tools"])
    first_version = master["version"]

    repeated = client.get("/api/agent-studio/agents")
    repeated_master = next(agent for agent in repeated.json()["agents"] if agent["name"] == "Master Agent")
    assert repeated_master["version"] == first_version
    assert len(repeated_master["tool_permissions"]) == len(set(repeated_master["tool_permissions"]))

    login(client, USER_EMAIL)
    employee_response = client.get("/api/agent-studio/agents")
    assert employee_response.status_code == 200, employee_response.text
    employee_master = next(agent for agent in employee_response.json()["agents"] if agent["name"] == "Master Agent")
    assert set(employee_master["tool_permissions"]).isdisjoint(_MASTER_PROJECT_TOOLS)
    assert set(employee_master["approval_tools"]).isdisjoint(_MASTER_PROJECT_APPROVALS)
