"""Positions (job titles) and reporting lines: unlimited custom titles, archive without losing history,
cycle-free hierarchy, and none of it grants access."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login


def _users(client) -> dict[str, str]:
    return {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}


def _create(client, title: str, family: str = "other", level: int | None = None) -> dict:
    response = client.post("/api/admin/positions", json={"title": title, "job_family": family, "level": level})
    assert response.status_code == 201, response.text
    return response.json()


def test_admins_create_any_position_and_titles_are_unique_case_insensitively(admin_client):
    guard = _create(admin_client, "Night Shift Security Guard", "frontline", 9)
    _create(admin_client, "Chief Sustainability Officer", "executive", 1)
    assert admin_client.post("/api/admin/positions", json={"title": "night shift security guard"}).status_code == 409
    assert admin_client.post("/api/admin/positions", json={"title": "X"}).status_code == 422
    assert admin_client.post("/api/admin/positions", json={"title": "Pilot", "job_family": "astronaut"}).status_code == 422
    titles = [p["title"] for p in admin_client.get("/api/admin/positions").json()["positions"]]
    assert "Night Shift Security Guard" in titles and "Chief Sustainability Officer" in titles
    renamed = admin_client.patch(f"/api/admin/positions/{guard['id']}", json={"title": "Night Security Guard"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Night Security Guard"


def test_the_library_adds_missing_positions_once(admin_client):
    first = admin_client.post("/api/admin/positions/library/apply", json={"titles": ["Accountant", "Security Guard", "Intern"]}).json()
    assert sorted(first["created"]) == ["Accountant", "Intern", "Security Guard"]
    again = admin_client.post("/api/admin/positions/library/apply", json={"titles": ["Accountant"]}).json()
    assert again == {"created": [], "skipped": ["Accountant"]}
    library = {p["title"]: p for p in admin_client.get("/api/admin/positions/library").json()["positions"]}
    assert library["Accountant"]["added"] and not library["Chief Executive Officer (CEO)"]["added"]
    assert len(library) >= 60


def test_assignments_build_a_hierarchy_and_reject_loops(admin_client):
    ids = _users(admin_client)
    ceo, accountant = _create(admin_client, "CEO", "executive", 1), _create(admin_client, "Senior Accountant", "professional", 5)
    assert admin_client.put(f"/api/admin/users/{ids[ADMIN_EMAIL]}/org", json={"position_id": ceo["id"]}).status_code == 200
    first = admin_client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"position_id": accountant["id"], "reports_to_id": ids[ADMIN_EMAIL]})
    assert first.status_code == 200 and first.json()["position_title"] == "Senior Accountant" and first.json()["reports_to_id"] == ids[ADMIN_EMAIL]
    assert admin_client.put(f"/api/admin/users/{ids[USER2_EMAIL]}/org", json={"reports_to_id": ids[USER_EMAIL]}).status_code == 200

    # admin -> user -> user2: making the admin report to user2 would close a loop.
    loop = admin_client.put(f"/api/admin/users/{ids[ADMIN_EMAIL]}/org", json={"position_id": ceo["id"], "reports_to_id": ids[USER2_EMAIL]})
    assert loop.status_code == 422 and "loop" in loop.json()["error"]["message"]
    assert admin_client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"reports_to_id": ids[USER_EMAIL]}).status_code == 422

    chart = {p["email"]: p for p in admin_client.get("/api/org/chart").json()["people"]}
    assert chart[USER2_EMAIL]["reports_to_id"] == ids[USER_EMAIL]
    assert chart[ADMIN_EMAIL]["position_title"] == "CEO" and chart[ADMIN_EMAIL]["reports_to_id"] is None


def test_archiving_and_renaming_keep_history_and_current_holders(admin_client):
    ids = _users(admin_client)
    clerk = _create(admin_client, "Records Clerk", "administration", 9)
    admin_client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"position_id": clerk["id"]})
    admin_client.patch(f"/api/admin/positions/{clerk['id']}", json={"title": "Records Officer"})
    archived = admin_client.patch(f"/api/admin/positions/{clerk['id']}", json={"is_active": False}).json()
    assert archived["is_active"] is False and archived["holders"] == 1  # the holder keeps it

    # Archived positions cannot be given to someone new, and their title can be reused by a new position.
    assert admin_client.put(f"/api/admin/users/{ids[USER2_EMAIL]}/org", json={"position_id": clerk["id"]}).status_code == 422
    _create(admin_client, "Records Officer", "administration", 8)
    assert admin_client.patch(f"/api/admin/positions/{clerk['id']}", json={"is_active": True}).status_code == 409

    admin_client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"position_id": None})
    history = admin_client.get(f"/api/admin/users/{ids[USER_EMAIL]}/org-history").json()["entries"]
    assert [h["position_title"] for h in history] == [None, "Records Clerk"]  # titles as they read when assigned
    assert history[0]["ended_at"] is None and history[1]["ended_at"] is not None
    actions = [e["action"] for e in admin_client.get("/api/admin/audit-logs?limit=50").json()["entries"]]
    assert {"position.created", "position.updated", "position.archived", "user.org_assignment_changed"} <= set(actions)


def test_positions_never_grant_access_and_only_admins_manage_them(client):
    login(client, ADMIN_EMAIL)
    ids = _users(client)
    chief = _create(client, "Chief Information Officer", "executive", 1)
    client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"position_id": chief["id"]})

    login(client, USER_EMAIL)
    me = client.get("/api/auth/me").json()
    assert me["role"] != "admin"
    # An executive title gives no administrator access.
    for request in (
        client.get("/api/admin/users"),
        client.get("/api/admin/positions"),
        client.post("/api/admin/positions", json={"title": "Self-made Director"}),
        client.put(f"/api/admin/users/{ids[USER_EMAIL]}/org", json={"position_id": None}),
    ):
        assert request.status_code == 403
    # Everyone signed in can read the chart; nobody signed out can.
    people = client.get("/api/org/chart").json()["people"]
    assert any(p["email"] == USER_EMAIL and p["position_title"] == "Chief Information Officer" for p in people)
    client.post("/api/auth/logout")
    assert client.get("/api/org/chart").status_code == 401
