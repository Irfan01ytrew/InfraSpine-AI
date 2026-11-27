"""Approval limits for purchase requests and expense claims: authority by position or person, configured
separately from roles; no self-approval; escalation suggestion up the reporting line."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login


def _setup(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    users = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{users[USER2_EMAIL]}", json={"role": "manager", "department_ids": [finance["id"]]})
    client.patch(f"/api/admin/users/{users[USER_EMAIL]}", json={"role": "employee", "department_ids": [finance["id"]]})
    manager_position = client.post("/api/admin/positions", json={"title": "Finance Manager", "job_family": "management"}).json()
    client.put(f"/api/admin/users/{users[USER2_EMAIL]}/org", json={"position_id": manager_position["id"], "reports_to_id": users[ADMIN_EMAIL]})
    client.put(f"/api/admin/users/{users[USER_EMAIL]}/org", json={"reports_to_id": users[USER2_EMAIL]})
    limit = client.post("/api/admin/approval-limits", json={"category": "purchase", "position_id": manager_position["id"], "max_amount": "50000", "currency": "PKR"})
    assert limit.status_code == 201, limit.text
    return finance, users


def _request(client, finance, amount: str, *, category: str = "purchase", currency: str = "PKR"):
    response = client.post("/api/service-requests", json={
        "category": category, "department_group_id": finance["id"], "subject": f"Buy items for {amount}",
        "description": "Synthetic purchase request.", "amount": amount, "currency": currency,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _approve(client, request_id: str):
    return client.post(f"/api/service-requests/{request_id}/triage", json={"status": "resolved", "comment": "Approved."})


def test_a_manager_approves_within_their_positions_limit_but_not_above_it(client):
    finance, users = _setup(client)
    login(client, USER_EMAIL)
    small, large = _request(client, finance, "30000"), _request(client, finance, "120000")

    login(client, USER2_EMAIL)
    listed = {item["id"]: item for item in client.get("/api/service-requests").json()["items"]}
    assert listed[small["id"]]["approval"]["can_approve"] is True
    assert listed[large["id"]]["approval"]["can_approve"] is False
    assert "your limit is PKR 50,000.00" in listed[large["id"]]["approval"]["reason"]
    assert _approve(client, small["id"]).status_code == 200
    denied = _approve(client, large["id"])
    assert denied.status_code == 403 and "needs PKR 120,000.00" in denied.json()["error"]["message"]
    # Rejecting needs no authority.
    assert client.post(f"/api/service-requests/{large['id']}/triage", json={"status": "rejected", "comment": "Too much."}).status_code == 200


def test_escalation_is_suggested_up_the_reporting_line_and_admins_need_a_limit_too(client):
    finance, users = _setup(client)
    login(client, USER_EMAIL)
    large = _request(client, finance, "120000")

    login(client, ADMIN_EMAIL)
    assert _approve(client, large["id"]).status_code == 403  # no limit configured for the administrator
    client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": users[ADMIN_EMAIL], "max_amount": "1000000", "currency": "PKR"})

    login(client, USER2_EMAIL)
    item = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == large["id"])
    assert item["approval"]["suggested_approver_email"] == ADMIN_EMAIL

    login(client, ADMIN_EMAIL)
    assert _approve(client, large["id"]).status_code == 200
    approved = next(e for e in client.get("/api/admin/audit-logs?limit=30").json()["entries"] if e["action"] == "service_request.approved")
    assert approved["details"]["limit"] == "1000000.00" and approved["details"]["limit_source"] == "personal"


def test_nobody_approves_their_own_request_and_currencies_must_match(client):
    finance, users = _setup(client)
    login(client, USER2_EMAIL)
    own = _request(client, finance, "100")
    denied = _approve(client, own["id"])
    assert denied.status_code == 403 and "own request" in denied.json()["error"]["message"]

    login(client, USER_EMAIL)
    dollars = _request(client, finance, "100", currency="USD")
    login(client, USER2_EMAIL)
    mismatch = _approve(client, dollars["id"])
    assert mismatch.status_code == 403 and "in PKR" in mismatch.json()["error"]["message"]


def test_amounts_are_validated_and_limits_are_admin_only(client):
    finance, users = _setup(client)
    login(client, USER_EMAIL)
    base = {"department_group_id": finance["id"], "subject": "x", "description": "y"}
    assert client.post("/api/service-requests", json={**base, "category": "purchase"}).status_code == 422  # no amount
    assert client.post("/api/service-requests", json={**base, "category": "it", "amount": "10", "currency": "PKR"}).status_code == 422
    assert client.post("/api/service-requests", json={**base, "category": "expense", "amount": "-5", "currency": "PKR"}).status_code == 422
    assert client.get("/api/admin/approval-limits").status_code == 403
    assert client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": users[USER_EMAIL], "max_amount": "1", "currency": "PKR"}).status_code == 403

    login(client, ADMIN_EMAIL)
    limits = client.get("/api/admin/approval-limits").json()["limits"]
    assert [(l["position_title"], l["max_amount"], l["currency"]) for l in limits] == [("Finance Manager", "50000.00", "PKR")]
    duplicate = client.post("/api/admin/approval-limits", json={"category": "purchase", "position_id": limits[0]["position_id"], "max_amount": "9", "currency": "PKR"})
    assert duplicate.status_code == 409
    removed = client.patch(f"/api/admin/approval-limits/{limits[0]['id']}", json={"is_active": False})
    assert removed.status_code == 200 and removed.json()["limits"] == []
