"""PostgreSQL acceptance for grant-bound, independently reviewed agent templates."""

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, CSRF, USER_EMAIL, USER2_EMAIL, login, run_db


def _grant(client, email: str, permission: str):
    response = client.post(
        "/api/agent-catalog/grants",
        json={"email": email, "permission": permission},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_template(client, *, slug="operations-research", name="Operations research helper"):
    return client.post(
        "/api/agent-catalog/templates",
        json={
            "slug": slug,
            "name": name,
            "summary": "Find internal procedures the requester may read.",
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )


def _set_catalog_grants(client):
    login(client, ADMIN_EMAIL)
    _grant(client, USER_EMAIL, "publish")
    _grant(client, USER_EMAIL, "review")
    _grant(client, USER2_EMAIL, "review")
    login(client, USER_EMAIL)


def _submit_and_approve(client, version_id: str):
    submitted = client.post(f"/api/agent-catalog/versions/{version_id}/submit", headers=CSRF)
    assert submitted.status_code == 200, submitted.text
    login(client, USER2_EMAIL)
    decision = client.post(
        f"/api/agent-catalog/versions/{version_id}/review",
        json={"decision": "approve", "rationale": "The configuration matches the reviewed scope."},
        headers=CSRF,
    )
    assert decision.status_code == 200, decision.text
    login(client, USER_EMAIL)
    return decision.json()


def test_catalog_requires_grants_and_independent_review_before_publication(client):
    login(client, USER_EMAIL)
    assert client.get("/api/agent-catalog").status_code == 200
    assert client.get("/api/agent-catalog/manage").status_code == 403
    assert _create_template(client).status_code == 403

    login(client, ADMIN_EMAIL)
    _grant(client, USER_EMAIL, "publish")
    _grant(client, USER_EMAIL, "review")
    _grant(client, USER2_EMAIL, "review")
    login(client, USER_EMAIL)

    created = _create_template(client)
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["version"] == 1 and draft["status"] == "draft"
    assert client.get("/api/agent-catalog").json()["items"] == []

    submitted = client.post(f"/api/agent-catalog/versions/{draft['version_id']}/submit", headers=CSRF)
    assert submitted.status_code == 200 and submitted.json()["status"] == "pending_review"
    self_review = client.post(
        f"/api/agent-catalog/versions/{draft['version_id']}/review",
        json={"decision": "approve", "rationale": "Self review should be refused."},
        headers=CSRF,
    )
    assert self_review.status_code == 403
    assert client.get("/api/agent-catalog").json()["items"] == []

    login(client, USER2_EMAIL)
    approved = client.post(
        f"/api/agent-catalog/versions/{draft['version_id']}/review",
        json={"decision": "approve", "rationale": "The configuration is within the allowlisted capability."},
        headers=CSRF,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewed_by_user_id"]

    login(client, USER_EMAIL)
    public = client.get("/api/agent-catalog").json()
    assert public["can_publish"] is True and public["can_review"] is True
    assert [(row["slug"], row["version"], row["tool_policy"]) for row in public["items"]] == [
        ("operations-research", 1, ["knowledge_search"])
    ]
    async def read_catalog_audit(conn):
        result = await conn.execute(
            text("SELECT action, details::text FROM audit_logs WHERE action LIKE 'agent_catalog.%' ORDER BY id")
        )
        return result.all()

    admin_events = run_db(read_catalog_audit)
    assert {row[0] for row in admin_events} >= {
        "agent_catalog.grant_created", "agent_catalog.template_created", "agent_catalog.version_submitted",
        "agent_catalog.version_approved",
    }
    assert "Find internal procedures" not in " ".join(row[1] for row in admin_events)


def test_catalog_versions_preserve_prior_publication_then_revoke_without_erasing_history(client):
    _set_catalog_grants(client)
    first = _create_template(client).json()
    _submit_and_approve(client, first["version_id"])
    assert client.get("/api/agent-catalog").json()["items"][0]["version"] == 1

    second = client.post(
        f"/api/agent-catalog/templates/{first['template_id']}/versions",
        json={
            "name": "Operations research helper, updated",
            "summary": "Use the same requester ACL and preserve citations.",
            "agent_type": "knowledge_research",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["knowledge_search"],
        },
        headers=CSRF,
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["version_id"]
    assert client.get("/api/agent-catalog").json()["items"][0]["version"] == 1
    _submit_and_approve(client, second_id)

    public = client.get("/api/agent-catalog").json()["items"]
    assert len(public) == 1 and public[0]["version"] == 2
    managed = client.get("/api/agent-catalog/manage").json()["items"][0]
    assert managed["published_version"] == 2
    assert [(item["version"], item["status"]) for item in managed["versions"]] == [
        (2, "approved"), (1, "superseded")
    ]

    login(client, USER2_EMAIL)
    revoked = client.post(
        f"/api/agent-catalog/templates/{first['template_id']}/revoke",
        json={"rationale": "The reviewed configuration is no longer suitable."},
        headers=CSRF,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] and revoked.json()["revoke_rationale"]
    assert client.get("/api/agent-catalog").json()["items"] == []
    assert client.get("/api/agent-catalog/manage").json()["items"][0]["revoked_at"]

    with pytest.raises(Exception, match="version definitions are immutable"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE internal_agent_catalog_versions SET summary = 'tampered' WHERE id = :id"),
                {"id": second_id},
            )
        )
    with pytest.raises(Exception, match="event history is append-only"):
        run_db(
            lambda conn: conn.execute(
                text("DELETE FROM internal_agent_catalog_events WHERE event_type = 'catalog_revoked'")
            )
        )


def test_catalog_invalid_tools_and_revoked_publisher_grant_fail_closed(client):
    login(client, ADMIN_EMAIL)
    grant = _grant(client, USER_EMAIL, "publish")
    duplicate = client.post(
        "/api/agent-catalog/grants",
        json={"email": USER_EMAIL, "permission": "publish"},
        headers=CSRF,
    )
    assert duplicate.status_code == 409

    login(client, USER_EMAIL)
    invalid = client.post(
        "/api/agent-catalog/templates",
        json={
            "slug": "unsafe-training-template",
            "name": "Training with unregistered capability",
            "summary": "This tool does not belong to this agent type.",
            "agent_type": "employee_training",
            "knowledge_scope": "requester_acl",
            "tool_policy": ["public_web_research"],
        },
        headers=CSRF,
    )
    assert invalid.status_code == 422

    login(client, ADMIN_EMAIL)
    revoked = client.post(f"/api/agent-catalog/grants/{grant['id']}/revoke", headers=CSRF)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    login(client, USER_EMAIL)
    assert client.get("/api/agent-catalog").json()["can_publish"] is False
    assert client.get("/api/agent-catalog/manage").status_code == 403
    assert _create_template(client, slug="revoked-publisher", name="Revoked publisher attempt").status_code == 403
