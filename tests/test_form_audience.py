"""Who may use a request form: everyone, or listed departments (members and managers) and positions."""

import uuid

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login

FIELDS = [{"label": "Details", "type": "text", "required": True}]


def _names(client):
    return [f["name"] for f in client.get("/api/service-requests/forms").json()["forms"]]


def _submit(client, form_id):
    return client.post("/api/service-requests", json={"category": "other", "subject": "x", "form_id": form_id, "answers": {"details": "y"}})


def test_forms_are_offered_and_accepted_only_for_their_audience(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    ids = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{ids[USER_EMAIL]}", json={"role": "employee", "department_ids": [finance["id"]]})
    driver = client.post("/api/admin/positions", json={"title": "Driver", "job_family": "operations"}).json()
    client.put(f"/api/admin/users/{ids[USER2_EMAIL]}/org", json={"position_id": driver["id"]})

    open_form = client.post("/api/admin/request-forms", json={"name": "Open form", "category": "other", "fields": FIELDS}).json()["forms"][0]
    by_dept = client.post("/api/admin/request-forms", json={"name": "Finance only", "category": "other", "fields": FIELDS,
                                                          "audience_department_ids": [finance["id"]]})
    assert by_dept.status_code == 201, by_dept.text
    by_dept = next(f for f in by_dept.json()["forms"] if f["name"] == "Finance only")
    assert by_dept["audience_departments"] == [{"id": finance["id"], "name": "Finance"}] and by_dept["audience_positions"] == []
    by_pos = next(f for f in client.post("/api/admin/request-forms", json={"name": "Drivers only", "category": "other", "fields": FIELDS,
                  "audience_position_ids": [driver["id"]]}).json()["forms"] if f["name"] == "Drivers only")
    assert by_pos["audience_positions"] == [{"id": driver["id"], "name": "Driver"}]
    assert client.post("/api/admin/request-forms", json={"name": "Bad", "category": "other", "fields": FIELDS,
                       "audience_department_ids": [str(uuid.uuid4())]}).status_code == 422
    assert sorted(_names(client)) == ["Drivers only", "Finance only", "Open form"]  # admins see every form

    login(client, USER_EMAIL)  # Finance member, not a driver
    assert sorted(_names(client)) == ["Finance only", "Open form"]
    assert _submit(client, by_dept["id"]).status_code == 201
    refused = _submit(client, by_pos["id"])
    assert refused.status_code == 422 and "no longer available" in refused.json()["error"]["message"]

    login(client, USER2_EMAIL)  # a driver outside Finance
    assert sorted(_names(client)) == ["Drivers only", "Open form"]
    assert _submit(client, by_pos["id"]).status_code == 201 and _submit(client, by_dept["id"]).status_code == 422
    assert _submit(client, open_form["id"]).status_code == 201

    login(client, ADMIN_EMAIL)  # opening a form to everyone again
    reopened = client.put(f"/api/admin/request-forms/{by_dept['id']}", json={"name": "Finance only", "category": "other", "fields": FIELDS})
    assert next(f for f in reopened.json()["forms"] if f["id"] == by_dept["id"])["audience_departments"] == []
    login(client, USER2_EMAIL)
    assert "Finance only" in _names(client)


def test_department_managers_may_use_their_departments_forms(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    ids = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{ids[USER2_EMAIL]}", json={"role": "manager", "department_ids": [finance["id"]]})
    client.post("/api/admin/request-forms", json={"name": "Finance only", "category": "other", "fields": FIELDS,
                                                 "audience_department_ids": [finance["id"]]})
    login(client, USER2_EMAIL)
    assert _names(client) == ["Finance only"]
    login(client, USER_EMAIL)
    assert _names(client) == []
