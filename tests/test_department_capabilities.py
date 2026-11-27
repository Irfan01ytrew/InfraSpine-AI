"""Department-scoped delegable capabilities: people.invite, agents.manage and training.manage."""

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login

AGENT = {
    "name": "Finance knowledge researcher",
    "agent_type": "knowledge_research",
    "knowledge_scope": "department_only",
    "tool_policy": ["knowledge_search"],
}


def _session(client, email: str) -> dict[str, str]:
    client.cookies.clear()
    login(client, email)
    return dict(client.cookies)


def _as(client, cookies: dict[str, str]):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


def _setup(client):
    user = _session(client, USER_EMAIL)
    admin = _session(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    operations = client.post("/api/admin/access/groups", json={"name": "Operations", "kind": "department"}).json()
    return user, admin, finance, operations


def _grant(client, admin, permission: str, scope_group_id):
    response = _as(client, admin).post("/api/admin/access/grants", json={
        "email": USER_EMAIL, "permission": permission, "category": None, "scope_group_id": scope_group_id,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_people_invite_lets_an_employee_invite_employees_only_into_departments_in_scope(client):
    user, admin, finance, operations = _setup(client)
    # No manager role and no grant: refused outright; with a grant, departments outside its scope are hidden (404).
    assert _as(client, user).post(f"/api/departments/{finance['id']}/invitations", json={"email": "a@new.test"}).status_code == 403
    assert _as(client, user).get("/api/departments/invitable").json()["groups"] == []

    grant_id = _grant(client, admin, "people.invite", finance["id"])
    hr = _as(client, user)
    assert "people.invite" in hr.get("/api/auth/me").json()["capabilities"]
    assert [g["name"] for g in hr.get("/api/departments/invitable").json()["groups"]] == ["Finance"]
    invited = hr.post(f"/api/departments/{finance['id']}/invitations", json={"email": "a@new.test"})
    assert invited.status_code == 201, invited.text
    assert invited.json()["role"] == "employee" and invited.json()["department_ids"] == [finance["id"]]
    assert hr.post(f"/api/departments/{operations['id']}/invitations", json={"email": "b@new.test"}).status_code == 404
    assert [i["email"] for i in hr.get(f"/api/departments/{finance['id']}/invitations").json()["invitations"]] == ["a@new.test"]
    assert hr.get("/api/admin/users").status_code == 403  # no administrator area opens

    assert _as(client, admin).post(f"/api/admin/access/grants/{grant_id}/revoke").status_code == 200
    assert _as(client, user).post(f"/api/departments/{finance['id']}/invitations", json={"email": "c@new.test"}).status_code == 403


def test_unscoped_people_invite_covers_every_local_department(client):
    user, admin, finance, operations = _setup(client)
    _grant(client, admin, "people.invite", None)
    names = [g["name"] for g in _as(client, user).get("/api/departments/invitable").json()["groups"]]
    assert {"Finance", "Operations"} <= set(names)
    assert _as(client, user).post(f"/api/departments/{operations['id']}/invitations", json={"email": "d@new.test"}).status_code == 201


def test_agents_manage_allows_creating_and_disabling_agents_in_scope_only(client):
    user, admin, finance, operations = _setup(client)
    refused = _as(client, user).post("/api/agents", json={**AGENT, "department_group_id": finance["id"]})
    assert refused.status_code == 403

    _grant(client, admin, "agents.manage", finance["id"])
    owner = _as(client, user)
    assert [d["name"] for d in owner.get("/api/agents/departments").json()["departments"]] == ["Finance"]
    created = owner.post("/api/agents", json={**AGENT, "department_group_id": finance["id"]})
    assert created.status_code == 201, created.text
    assert created.json()["can_manage"] is True
    assert owner.post("/api/agents", json={**AGENT, "department_group_id": operations["id"]}).status_code == 403
    disabled = owner.post(f"/api/agents/{created.json()['id']}/disable")
    assert disabled.status_code in (200, 201), disabled.text
    listed = owner.get("/api/agents").json()["agents"]
    assert any(a["id"] == created.json()["id"] and a["enabled"] is False for a in listed)  # managers see disabled ones


def test_training_manage_opens_department_training_views_in_scope_only(client):
    user, admin, finance, operations = _setup(client)
    assert _as(client, user).get(f"/api/rag/training-assignments?department_id={finance['id']}").status_code == 403

    _grant(client, admin, "training.manage", finance["id"])
    trainer = _as(client, user)
    assert trainer.get(f"/api/rag/training-assignments?department_id={finance['id']}").status_code == 200
    assert trainer.get(f"/api/rag/training-assignment-candidates?department_id={finance['id']}").status_code == 200
    assert trainer.get(f"/api/rag/training-assignments?department_id={operations['id']}").status_code == 404


def test_department_capabilities_reject_category_scope(client):
    _user, admin, finance, _operations = _setup(client)
    response = _as(client, admin).post("/api/admin/access/grants", json={
        "email": USER_EMAIL, "permission": "people.invite", "category": "financial", "scope_group_id": finance["id"],
    })
    assert response.status_code == 422
