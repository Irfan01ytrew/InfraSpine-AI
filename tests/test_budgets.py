"""Department budgets: approved and committed spend, blocking and warning budgets, and who can see them."""

from datetime import date, timedelta

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login

TODAY = date.today()


def _setup(client, enforcement="block"):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    ids = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{ids[USER2_EMAIL]}", json={"role": "manager", "department_ids": [finance["id"]]})
    client.patch(f"/api/admin/users/{ids[USER_EMAIL]}", json={"role": "employee", "department_ids": [finance["id"]]})
    client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": ids[USER2_EMAIL], "max_amount": "500000", "currency": "PKR"})
    created = client.post("/api/org/budgets", json={
        "name": "Finance purchases this quarter", "department_group_id": finance["id"], "category": "purchase",
        "period_start": str(TODAY - timedelta(days=10)), "period_end": str(TODAY + timedelta(days=80)),
        "amount": "100000", "currency": "PKR", "enforcement": enforcement,
    })
    assert created.status_code == 201, created.text
    return finance, created.json()["budgets"][0]


def _submit(client, finance, amount, currency="PKR"):
    response = client.post("/api/service-requests", json={
        "category": "purchase", "department_group_id": finance["id"], "subject": f"Buy {amount}",
        "description": "Synthetic.", "amount": amount, "currency": currency,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _approve(client, request_id):
    return client.post(f"/api/service-requests/{request_id}/triage", json={"status": "resolved", "comment": "ok"})


def _budget(client, budget_id):
    return next(b for b in client.get("/api/org/budgets").json()["budgets"] if b["id"] == budget_id)


def test_a_blocking_budget_stops_an_approval_that_would_exceed_it(client):
    finance, budget = _setup(client, "block")
    login(client, USER_EMAIL)
    first, second = _submit(client, finance, "60000"), _submit(client, finance, "50000")
    _submit(client, finance, "99999", currency="USD")  # other currency: not counted

    login(client, USER2_EMAIL)
    use = _budget(client, budget["id"])
    assert (use["approved"], use["committed"]) == ("0.00", "110000.00")
    assert _approve(client, first["id"]).status_code == 200
    use = _budget(client, budget["id"])
    assert (use["approved"], use["committed"], use["remaining"]) == ("60000.00", "50000.00", "40000.00")

    line = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == second["id"])["budgets"][0]
    assert line["would_exceed"] is True and line["after_approval"] == "110000.00"
    denied = _approve(client, second["id"])
    assert denied.status_code == 403 and "exceed the budget" in denied.json()["error"]["message"]
    # Rejected requests release the commitment.
    client.post(f"/api/service-requests/{second['id']}/triage", json={"status": "rejected", "comment": "Over budget"})
    assert _budget(client, budget["id"])["committed"] == "0.00"


def test_a_warning_budget_allows_the_overrun_and_records_it(client):
    finance, budget = _setup(client, "warn")
    login(client, USER_EMAIL)
    big = _submit(client, finance, "130000")
    login(client, USER2_EMAIL)
    assert _approve(client, big["id"]).status_code == 200
    assert _budget(client, budget["id"])["remaining"] == "-30000.00"
    login(client, ADMIN_EMAIL)
    approved = next(e for e in client.get("/api/admin/audit-logs?limit=20").json()["entries"] if e["action"] == "service_request.approved")
    assert approved["details"]["over_budget"] == [{"budget": "Finance purchases this quarter", "over_by": "30000.00"}]


def test_only_admins_manage_budgets_and_managers_see_their_departments(client):
    finance, budget = _setup(client)
    login(client, USER2_EMAIL)
    assert [b["name"] for b in client.get("/api/org/budgets").json()["budgets"]] == ["Finance purchases this quarter"]
    assert client.patch(f"/api/org/budgets/{budget['id']}", json={"amount": "999999"}).status_code == 403
    login(client, USER_EMAIL)
    assert client.get("/api/org/budgets").status_code == 403
    login(client, ADMIN_EMAIL)
    changed = client.patch(f"/api/org/budgets/{budget['id']}", json={"amount": "150000", "enforcement": "warn"}).json()["budgets"][0]
    assert (changed["amount"], changed["enforcement"]) == ("150000.00", "warn")
    assert client.patch(f"/api/org/budgets/{budget['id']}", json={"is_active": False}).json()["budgets"] == []
    bad = client.post("/api/org/budgets", json={"name": "Backwards", "department_group_id": finance["id"], "period_start": str(TODAY), "period_end": str(TODAY - timedelta(days=1)), "amount": "1", "currency": "PKR"})
    assert bad.status_code == 422
