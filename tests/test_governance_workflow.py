"""Governance approvals are scoped, two-person and separate from indexed knowledge."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, login


def test_scoped_reviewer_can_decide_but_claim_remains_unverified(client):
    login(client, ADMIN_EMAIL)
    grant = client.post(
        "/api/admin/access/grants",
        json={"email": USER2_EMAIL, "permission": "knowledge.review", "category": "general"},
    )
    assert grant.status_code == 201, grant.text

    login(client, "user@example.com")
    claim = client.post(
        "/api/claims",
        json={"kind": "proposal", "statement": "The standard maintenance window is Sunday morning.", "category": "general", "sources": []},
    )
    assert claim.status_code == 201, claim.text

    login(client, USER2_EMAIL)
    queue = client.get("/api/governance/claims").json()["items"]
    assert [row["id"] for row in queue] == [claim.json()["id"]]
    decision = client.post(
        f"/api/governance/claims/{claim.json()['id']}/decisions",
        json={"stage": "review", "rationale": "Checked the submitted operational source."},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["stage"] == "review"

    # A review-only grant cannot approve publication; the claimant remains the only one who can see their claim.
    assert client.post(
        f"/api/governance/claims/{claim.json()['id']}/decisions",
        json={"stage": "publish", "rationale": "Approved for publication."},
    ).status_code == 403
    assert client.get("/api/governance/claims").json()["items"] == []

    login(client, "user@example.com")
    own = client.get("/api/claims").json()["items"]
    assert own[0]["verified"] is False and own[0]["status"] == "submitted"

