"""Invitations and CSV imports can carry a position (job title) and a manager, applied at sign-up."""

from tests.conftest import ADMIN_EMAIL, CSRF, USER_EMAIL, login


def _setup(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    accountant = client.post("/api/admin/positions", json={"title": "Accountant", "job_family": "professional", "level": 6}).json()
    users = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    return finance, accountant, users


def _accept(client, token: str):
    client.cookies.clear()
    accepted = client.post("/api/onboarding/accept", json={"token": token, "password": "new-hire-password-123"}, headers=CSRF)
    assert accepted.status_code in (200, 201), accepted.text
    client.cookies.clear()
    login(client, ADMIN_EMAIL)


def _person(client, email: str) -> dict:
    return next(p for p in client.get("/api/org/chart").json()["people"] if p["email"] == email)


def test_invitation_position_and_manager_are_applied_at_sign_up(client):
    finance, accountant, users = _setup(client)
    invited = client.post("/api/admin/users/invitations", json={
        "email": "new.accountant@new.test", "department_ids": [finance["id"]],
        "position_id": accountant["id"], "reports_to_id": users[USER_EMAIL],
    })
    assert invited.status_code == 201, invited.text
    assert invited.json()["position_id"] == accountant["id"] and invited.json()["reports_to_id"] == users[USER_EMAIL]

    _accept(client, invited.json()["token"])
    person = _person(client, "new.accountant@new.test")
    assert person["position_title"] == "Accountant" and person["reports_to_id"] == users[USER_EMAIL]
    assert person["role"] == "employee"  # the position changed no access
    history = client.get(f"/api/admin/users/{person['user_id']}/org-history").json()["entries"]
    assert [(h["position_title"], h["reports_to_email"]) for h in history] == [("Accountant", USER_EMAIL)]


def test_a_position_archived_before_sign_up_is_skipped_not_fatal(client):
    finance, accountant, users = _setup(client)
    invited = client.post("/api/admin/users/invitations", json={"email": "late.joiner@new.test", "position_id": accountant["id"]}).json()
    client.patch(f"/api/admin/positions/{accountant['id']}", json={"is_active": False})
    _accept(client, invited["token"])
    assert _person(client, "late.joiner@new.test")["position_id"] is None
    accepted = next(e for e in client.get("/api/admin/audit-logs?limit=20").json()["entries"] if e["action"] == "onboarding.accepted")
    assert accepted["details"]["position_skipped"] == "archived"


def test_invitation_placement_is_validated(client):
    finance, accountant, users = _setup(client)
    client.patch(f"/api/admin/positions/{accountant['id']}", json={"is_active": False})
    assert client.post("/api/admin/users/invitations", json={"email": "a@new.test", "position_id": accountant["id"]}).status_code == 422
    assert client.post("/api/admin/users/invitations", json={"email": "b@new.test", "reports_to_id": "00000000-0000-0000-0000-000000000000"}).status_code == 422


def test_csv_preview_resolves_position_and_manager_columns(client):
    finance, accountant, users = _setup(client)
    csv_text = (
        "email,role,departments,position,reports_to\n"
        f"one@new.test,employee,Finance,accountant,{USER_EMAIL.upper()}\n"
        "two@new.test,employee,Finance,Astronaut,\n"
        "three@new.test,employee,Finance,,nobody@new.test\n"
        "four@new.test,employee,Finance,,\n"
    )
    preview = client.post("/api/admin/users/invitations/csv-preview", json={"csv_text": csv_text})
    assert preview.status_code == 200, preview.text
    rows = {r["email"]: r for r in preview.json()["rows"]}
    assert rows["one@new.test"]["valid"] and rows["one@new.test"]["position_title"] == "Accountant"
    assert rows["one@new.test"]["position_id"] == accountant["id"] and rows["one@new.test"]["reports_to_email"] == USER_EMAIL
    assert not rows["two@new.test"]["valid"] and "Position 'Astronaut' does not exist" in rows["two@new.test"]["errors"][0]
    assert not rows["three@new.test"]["valid"] and "not an active account" in rows["three@new.test"]["errors"][0]
    assert rows["four@new.test"]["valid"] and rows["four@new.test"]["position_id"] is None
    bad = client.post("/api/admin/users/invitations/csv-preview", json={"csv_text": "email,salary\nx@new.test,1\n"})
    assert bad.status_code == 422 and "position and reports_to" in bad.json()["error"]["message"]
