"""Parallel approval stages: steps decided at the same time by all (all-of) or any one (any-of) of their approvers."""

from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login
from tests.test_approval_chains import _decide, _setup, _submit
from tests.test_enterprise_people import _invite_manager


def _add(client, label, kind, **extra):
    response = client.post("/api/admin/approval-chain", json={"category": "purchase", "label": label, "approver_kind": kind, **extra})
    assert response.status_code == 201, response.text
    return response.json()["steps"]


def _third_person(client, finance):
    """manager@example.com: a second Finance manager with their own PKR 1M purchase limit."""
    login(client, ADMIN_EMAIL)
    invitation = _invite_manager(client, finance["id"])
    assert client.post("/api/onboarding/accept", json={"token": invitation["token"], "password": "manager-password-123"}, headers=CSRF).status_code == 200
    third = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == "manager@example.com")
    client.post("/api/admin/approval-limits", json={"category": "purchase", "user_id": third, "max_amount": "1000000", "currency": "PKR"})
    return third


def test_an_all_of_stage_needs_every_approver_before_the_next_stage(client):
    finance, ids, _ = _setup(client)
    third = _third_person(client, finance)
    _add(client, "Manager review", "manager")
    _add(client, "Treasury", "person", user_id=third, parallel_with_previous=True, stage_mode="all")
    steps = _add(client, "CEO sign-off", "person", user_id=ids[ADMIN_EMAIL])
    assert [(s["label"], s["parallel_with_previous"], s["stage_mode"]) for s in steps] == [
        ("Manager review", False, "all"), ("Treasury", True, "all"), ("CEO sign-off", False, "all")]

    login(client, USER_EMAIL)
    request = _submit(client, finance, "40000")
    assert [(s["stage"], s["status"]) for s in request["approval_steps"]] == [(1, "pending"), (1, "pending"), (2, "waiting")]

    login(client, "manager@example.com", "manager-password-123")
    after_one = _decide(client, request["id"]).json()  # Treasury approves first: order inside a stage does not matter
    assert [s["status"] for s in after_one["approval_steps"]] == ["pending", "approved", "waiting"]
    login(client, ADMIN_EMAIL)
    assert _decide(client, request["id"]).status_code == 403  # the CEO stage is not reached yet
    login(client, USER2_EMAIL)
    after_both = _decide(client, request["id"]).json()
    assert [s["status"] for s in after_both["approval_steps"]] == ["approved", "approved", "pending"]
    login(client, ADMIN_EMAIL)
    done = _decide(client, request["id"]).json()
    assert done["status"] == "resolved"
    assert next(e for e in client.get("/api/admin/audit-logs?limit=40").json()["entries"] if e["action"] == "service_request.approved")["details"]["steps"] == 3


def test_an_any_of_stage_completes_with_one_approval_and_a_rejection_rejects(client):
    finance, ids, _ = _setup(client)
    third = _third_person(client, finance)
    _add(client, "Manager review", "manager")
    _add(client, "Treasury", "person", user_id=third, parallel_with_previous=True, stage_mode="any")

    login(client, USER_EMAIL)
    first, second = _submit(client, finance, "30000"), _submit(client, finance, "35000")
    assert {s["stage_mode"] for s in first["approval_steps"]} == {"any"} and all(s["final"] for s in first["approval_steps"])

    login(client, "manager@example.com", "manager-password-123")
    done = _decide(client, first["id"]).json()
    assert done["status"] == "resolved" and [s["status"] for s in done["approval_steps"]] == ["cancelled", "approved"]

    login(client, USER2_EMAIL)
    rejected = _decide(client, second["id"], "reject", "Not needed").json()
    assert rejected["status"] == "rejected" and [s["status"] for s in rejected["approval_steps"]] == ["rejected", "cancelled"]


def test_stages_are_edited_and_one_person_never_decides_two_steps_of_a_stage(client):
    finance, ids, fm = _setup(client)
    _add(client, "Finance check A", "position", position_id=fm["id"])
    steps = _add(client, "Finance check B", "position", position_id=fm["id"], parallel_with_previous=True)
    first, second = steps
    assert client.patch(f"/api/admin/approval-chain/{first['id']}", json={"parallel_with_previous": True}).status_code == 422
    changed = client.patch(f"/api/admin/approval-chain/{second['id']}", json={"stage_mode": "any"}).json()["steps"]
    assert [s["stage_mode"] for s in changed] == ["any", "any"]
    split = client.patch(f"/api/admin/approval-chain/{second['id']}", json={"parallel_with_previous": False}).json()["steps"]
    assert [s["parallel_with_previous"] for s in split] == [False, False]
    client.patch(f"/api/admin/approval-chain/{second['id']}", json={"parallel_with_previous": True, "stage_mode": "all"})
    # Removing the stage's first step leaves the other as a stage of its own.
    remaining = client.delete(f"/api/admin/approval-chain/{first['id']}").json()["steps"]
    assert [(s["label"], s["parallel_with_previous"]) for s in remaining] == [("Finance check B", False)]
    _add(client, "Finance check C", "position", position_id=fm["id"], parallel_with_previous=True, stage_mode="all")

    login(client, USER_EMAIL)
    request = _submit(client, finance, "20000")
    login(client, USER2_EMAIL)  # the only Finance Manager: decides one step of the stage, not both
    assert _decide(client, request["id"]).status_code == 200
    item = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == request["id"])
    assert item["can_decide_step"] is False and "already decided" in item["step_blocker"]
    assert _decide(client, request["id"]).status_code == 403
