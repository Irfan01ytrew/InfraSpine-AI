"""Department-scoped employee expertise directory API acceptance tests."""

import json

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, PASSWORD, USER2_EMAIL, USER_EMAIL, login, run_db


def _department(client, name: str) -> dict:
    response = client.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
    assert response.status_code == 201, response.text
    return response.json()


def _add_member(client, department_id: str, email: str) -> None:
    response = client.post(f"/api/admin/access/groups/{department_id}/members", json={"email": email})
    assert response.status_code == 204, response.text


def _invite_manager(client, department_id: str) -> str:
    response = client.post(
        "/api/admin/users/invitations",
        json={"email": "expertise-manager@example.com", "role": "manager", "department_ids": [department_id]},
    )
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": token, "password": "expertise-manager-password-123"},
        headers=CSRF,
    )
    assert accepted.status_code == 200, accepted.text
    return "expertise-manager@example.com"


def test_expertise_profile_validation_audit_and_department_revocation(client):
    assert client.get("/api/expertise/me").status_code == 401
    login(client, ADMIN_EMAIL)
    finance = _department(client, "Finance")
    research = _department(client, "Research")
    _add_member(client, finance["id"], USER_EMAIL)
    _add_member(client, research["id"], USER2_EMAIL)
    _add_member(client, finance["id"], USER2_EMAIL)

    login(client, USER_EMAIL)
    profile = client.put("/api/expertise/me", json={"tags": ["  Financial Modeling ", "Python"]})
    assert profile.status_code == 200, profile.text
    assert profile.json()["tags"] == ["financial modeling", "python"]
    assert client.get("/api/expertise/me").json()["tags"] == profile.json()["tags"]
    duplicate = client.put("/api/expertise/me", json={"tags": ["Python", " python "]})
    assert duplicate.status_code == 422
    control = client.put("/api/expertise/me", json={"tags": ["python\nadmin"]})
    assert control.status_code == 422
    too_many = client.put("/api/expertise/me", json={"tags": [f"skill-{index}" for index in range(21)]})
    assert too_many.status_code == 422

    login(client, USER2_EMAIL)
    assert client.put("/api/expertise/me", json={"tags": ["Python"]}).status_code == 200

    # User search sees only users in the searcher's current department and only that
    # shared department name, even when the match belongs to another department too.
    login(client, USER_EMAIL)
    found = client.get("/api/expertise/search", params={"skill": "PYTHON"})
    assert found.status_code == 200, found.text
    assert found.json()["skill"] == "python"
    assert len(found.json()["matches"]) == 1
    match = found.json()["matches"][0]
    assert match["email"] == USER2_EMAIL
    assert match["shared_departments"] == ["Finance"]
    assert "Research" not in str(match)

    # Keep only the colleague's profile in the Manager's result set; the actor is
    # never listed, and the next check focuses on the employee whose ACL is revoked.
    assert client.put("/api/expertise/me", json={"tags": ["financial modeling"]}).status_code == 200
    login(client, ADMIN_EMAIL)
    manager_email = _invite_manager(client, finance["id"])
    login(client, manager_email, "expertise-manager-password-123")
    manager_result = client.get("/api/expertise/search", params={"skill": "python"}).json()
    assert [row["email"] for row in manager_result["matches"]] == [USER2_EMAIL]
    assert manager_result["matches"][0]["shared_departments"] == ["Finance"]

    login(client, ADMIN_EMAIL)
    users = client.get("/api/admin/users").json()["users"]
    user2_id = next(row["id"] for row in users if row["email"] == USER2_EMAIL)
    removed = client.delete(f"/api/admin/access/groups/{finance['id']}/members/{user2_id}")
    assert removed.status_code == 204, removed.text

    login(client, USER_EMAIL)
    after_revoke = client.get("/api/expertise/search", params={"skill": "python"}).json()
    assert after_revoke["matches"] == []
    login(client, manager_email, "expertise-manager-password-123")
    after_manager_revoke = client.get("/api/expertise/search", params={"skill": "python"}).json()
    assert after_manager_revoke["matches"] == []
    login(client, USER2_EMAIL)
    assert client.get("/api/expertise/me").json()["tags"] == ["python"]

    async def _read_audit_details(conn):
        result = await conn.execute(text("SELECT details::text FROM audit_logs WHERE action LIKE 'expertise.%'"))
        return result.scalars().all()

    audit_details = run_db(_read_audit_details)
    assert audit_details
    assert all("python" not in details and "financial modeling" not in details for details in audit_details)


def test_expertise_search_requires_bounded_exact_query(client):
    login(client, USER_EMAIL)
    assert client.get("/api/expertise/search").status_code == 422
    assert client.get("/api/expertise/search", params={"skill": "x"}).status_code == 422
    assert client.get("/api/expertise/search", params={"skill": "x" * 61}).status_code == 422
    empty_scope = client.get("/api/expertise/search", params={"skill": "confidential skill"})
    assert empty_scope.status_code == 200 and empty_scope.json()["matches"] == []

    async def _read_search_event(conn):
        result = await conn.execute(
            text("SELECT details::text FROM audit_logs WHERE action = 'expertise.directory_searched'")
        )
        return result.scalars().all()

    events = run_db(_read_search_event)
    assert len(events) == 1
    assert json.loads(events[0]) == {"result_count": 0, "query_length": len("confidential skill")}

