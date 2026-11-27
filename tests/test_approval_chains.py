"""Multi-step approval chains: ordered steps by manager, position or person, amount thresholds, separation of
duties, final-step authority, and steps fixed at submission."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login


def _setup(client):
    """Finance: USER (employee) reports to USER2 (Finance manager, PKR 50k limit) who reports to the admin (PKR 1M)."""
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    ids = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{ids[USER2_EMAIL]}", json={"role": "manager", "department_ids": [finance["id"]]})
    client.patch(f"/api/admin/users/{ids[USER_EMAIL]}", json={"role": "employee", "department_ids": [finance["id"]]})
    fm = client.post("/api/admin/positions", json={"title": "Finance Manager", "job_family": "management"}).json()
    client.put(f"/api/admin/users/{ids[USER2_EMAIL]}/org", json={"position_id": fm["id"], "reports_to_id": ids[ADMIN_EMAIL]})
    client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"reports_to_id": ids[USER2_EMAIL]})
    client.post("/api/admin/approval-limits", json={"category": "purchase", "position_id": fm["id"], "max_amount": "50000", "currency": "PKR"})
    client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": ids[ADMIN_EMAIL], "max_amount": "1000000", "currency": "PKR"})
    return finance, ids, fm


def _chain(client, *steps):
    for step in steps:
        response = client.post("/api/admin/approval-chain", json={"category": "purchase", **step})
        assert response.status_code == 201, response.text
    return response.json()["steps"]


def _submit(client, finance, amount):
    response = client.post("/api/service-requests", json={
        "category": "purchase", "department_group_id": finance["id"], "subject": f"Purchase {amount}",
        "description": "Synthetic.", "amount": amount, "currency": "PKR",
    })
    assert response.status_code == 201, response.text
    return response.json()


def _decide(client, request_id, decision="approve", comment="ok"):
    return client.post(f"/api/service-requests/{request_id}/approval-decision", json={"decision": decision, "comment": comment})


def test_amount_thresholds_pick_the_steps_and_steps_are_decided_in_order(client):
    finance, ids, _ = _setup(client)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"},
           {"label": "CEO sign-off", "approver_kind": "person", "user_id": ids[ADMIN_EMAIL], "min_amount": "100000"})
    login(client, USER_EMAIL)
    small, large = _submit(client, finance, "30000"), _submit(client, finance, "120000")
    assert [s["label"] for s in small["approval_steps"]] == ["Manager review"] and small["approval_steps"][0]["final"]
    assert [(s["label"], s["status"]) for s in large["approval_steps"]] == [("Manager review", "pending"), ("CEO sign-off", "waiting")]

    login(client, ADMIN_EMAIL)
    assert _decide(client, large["id"]).status_code == 403  # the CEO step is not reached yet
    assert client.post(f"/api/service-requests/{large['id']}/triage", json={"status": "resolved", "comment": "x"}).status_code == 409

    login(client, USER2_EMAIL)
    assert _decide(client, small["id"]).json()["status"] == "resolved"
    after_manager = _decide(client, large["id"], comment="Budget checked").json()
    assert after_manager["status"] == "in_progress"
    assert [s["status"] for s in after_manager["approval_steps"]] == ["approved", "pending"]

    login(client, ADMIN_EMAIL)
    done = _decide(client, large["id"]).json()
    assert done["status"] == "resolved" and [s["decided_by_email"] for s in done["approval_steps"]] == [USER2_EMAIL, ADMIN_EMAIL]
    approved = next(e for e in client.get("/api/admin/audit-logs?limit=30").json()["entries"] if e["action"] == "service_request.approved")
    assert approved["details"]["steps"] == 2


def test_a_rejection_closes_the_request_and_cancels_later_steps(client):
    finance, ids, _ = _setup(client)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"}, {"label": "CEO sign-off", "approver_kind": "person", "user_id": ids[ADMIN_EMAIL]})
    login(client, USER_EMAIL)
    request = _submit(client, finance, "70000")
    login(client, USER2_EMAIL)
    assert _decide(client, request["id"], "reject", "").status_code == 422  # a rejection needs a reason
    rejected = _decide(client, request["id"], "reject", "Not in budget").json()
    assert rejected["status"] == "rejected" and [s["status"] for s in rejected["approval_steps"]] == ["rejected", "cancelled"]


def test_one_person_cannot_decide_two_steps_and_the_final_step_needs_authority(client):
    finance, ids, fm = _setup(client)
    # USER2 is both the requester's manager and the Finance Manager.
    _chain(client, {"label": "Manager review", "approver_kind": "manager"}, {"label": "Finance check", "approver_kind": "position", "position_id": fm["id"]})
    login(client, USER_EMAIL)
    request = _submit(client, finance, "20000")
    login(client, USER2_EMAIL)
    assert _decide(client, request["id"]).status_code == 200
    second = _decide(client, request["id"])
    assert second.status_code == 403 and "earlier step" in second.json()["error"]["message"]
    view = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == request["id"])
    assert view["can_decide_step"] is False and "earlier step" in view["step_blocker"]


def test_a_step_approver_outside_the_department_can_see_and_decide_and_steps_are_fixed_at_submission(client):
    finance, ids, _ = _setup(client)
    login(client, ADMIN_EMAIL)
    client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": ids[USER_EMAIL], "max_amount": "10000", "currency": "PKR"})
    steps = _chain(client, {"label": "Peer check", "approver_kind": "person", "user_id": ids[USER_EMAIL]})
    login(client, USER2_EMAIL)  # the manager requests; the employee (not a manager) is the approver
    request = _submit(client, finance, "5000")

    login(client, ADMIN_EMAIL)  # changing the chain now does not touch the submitted request
    client.delete(f"/api/admin/approval-chain/{steps[0]['id']}")
    _chain(client, {"label": "CEO only", "approver_kind": "person", "user_id": ids[ADMIN_EMAIL]})

    login(client, USER_EMAIL)
    visible = {i["id"]: i for i in client.get("/api/service-requests").json()["items"]}
    assert request["id"] in visible and visible[request["id"]]["can_decide_step"] is True
    assert [s["label"] for s in visible[request["id"]]["approval_steps"]] == ["Peer check"]
    assert _decide(client, request["id"]).json()["status"] == "resolved"


def test_the_chain_is_admin_only_and_reorderable(client):
    finance, ids, fm = _setup(client)
    steps = _chain(client, {"label": "Manager review", "approver_kind": "manager"}, {"label": "Finance check", "approver_kind": "position", "position_id": fm["id"]})
    moved = client.post(f"/api/admin/approval-chain/{steps[1]['id']}/move", json={"direction": -1}).json()["steps"]
    assert [(s["step_order"], s["label"]) for s in moved] == [(1, "Finance check"), (2, "Manager review")]
    assert client.post("/api/admin/approval-chain", json={"category": "purchase", "label": "Bad", "approver_kind": "position"}).status_code == 422
    login(client, USER_EMAIL)
    assert client.get("/api/admin/approval-chain").status_code == 403
