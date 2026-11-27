"""Custom request forms: building forms, typed answers, routing, versions and access."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login

LAPTOP = {
    "name": "New laptop", "description": "For new joiners and replacements.", "category": "it",
    "fields": [
        {"label": "Model", "type": "choice", "required": True, "options": ["Standard", "Developer", "Developer"]},
        {"label": "Needed by", "type": "date", "required": True},
        {"label": "Quantity", "type": "number"},
        {"label": "Reason", "type": "long_text"},
        {"label": "I will return the old device", "type": "yes_no", "required": True},
    ],
}


def _plain(answers):
    return [{"label": a["label"], "value": a["value"]} for a in answers]


def _it_department(client):
    login(client, ADMIN_EMAIL)
    return client.post("/api/admin/access/groups", json={"name": "IT", "kind": "department"}).json()


def test_admins_build_forms_and_employees_submit_typed_answers_routed_to_the_department(client):
    it = _it_department(client)
    created = client.post("/api/admin/request-forms", json={**LAPTOP, "department_group_id": it["id"]})
    assert created.status_code == 201, created.text
    form = created.json()["forms"][0]
    assert [f["key"] for f in form["fields"]] == ["model", "needed_by", "quantity", "reason", "i_will_return_the_old_device"]
    assert form["fields"][0]["options"] == ["Standard", "Developer"] and form["department_name"] == "IT"
    assert client.post("/api/admin/request-forms", json=LAPTOP).status_code == 409  # one active form per name

    login(client, USER_EMAIL)  # not a member of IT
    assert client.post("/api/admin/request-forms", json=LAPTOP).status_code == 403
    assert [f["name"] for f in client.get("/api/service-requests/forms").json()["forms"]] == ["New laptop"]
    base = {"category": "other", "subject": "Laptop for new hire", "form_id": form["id"]}
    bad = [
        {"needed_by": "2026-12-01", "i_will_return_the_old_device": True},  # model missing
        {"model": "Gaming", "needed_by": "2026-12-01", "i_will_return_the_old_device": True},
        {"model": "Standard", "needed_by": "next week", "i_will_return_the_old_device": True},
        {"model": "Standard", "needed_by": "2026-12-01", "quantity": "two", "i_will_return_the_old_device": True},
        {"model": "Standard", "needed_by": "2026-12-01", "i_will_return_the_old_device": False},
        {"model": "Standard", "needed_by": "2026-12-01", "i_will_return_the_old_device": True, "colour": "red"},
    ]
    for answers in bad:
        assert client.post("/api/service-requests", json={**base, "answers": answers}).status_code == 422, answers
    made = client.post("/api/service-requests", json={**base, "answers": {
        "model": "Developer", "needed_by": "2026-12-01", "quantity": "1200", "i_will_return_the_old_device": True}})
    assert made.status_code == 201, made.text
    item = made.json()
    assert (item["category"], item["department_name"], item["form_name"]) == ("it", "IT", "New laptop")
    assert _plain(item["form_answers"]) == [
        {"label": "Model", "value": "Developer"}, {"label": "Needed by", "value": "2026-12-01"},
        {"label": "Quantity", "value": "1,200"}, {"label": "I will return the old device", "value": "Yes"}]
    assert item["description"].startswith("Model: Developer\nNeeded by: 2026-12-01")
    assert client.post("/api/service-requests", json={"category": "it", "subject": "x", "description": "", "answers": {"a": "b"}}).status_code == 422


def test_editing_a_form_starts_a_new_version_and_keeps_submitted_answers(client):
    login(client, ADMIN_EMAIL)
    form = client.post("/api/admin/request-forms", json={**LAPTOP, "category": "facilities"}).json()["forms"][0]
    login(client, USER_EMAIL)
    first = client.post("/api/service-requests", json={"category": "facilities", "subject": "Desk", "form_id": form["id"], "answers": {
        "model": "Standard", "needed_by": "2026-11-20", "i_will_return_the_old_device": True}}).json()

    login(client, ADMIN_EMAIL)
    renamed = {**LAPTOP, "category": "facilities", "fields": [{"label": "Device model", "type": "text", "required": True}]}
    updated = client.put(f"/api/admin/request-forms/{form['id']}", json=renamed).json()["forms"][0]
    assert updated["version"] == 2
    item = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == first["id"])
    assert _plain(item["form_answers"])[0] == {"label": "Model", "value": "Standard"}  # unchanged by the edit
    assert client.delete(f"/api/admin/request-forms/{form['id']}").json()["forms"] == []
    login(client, USER_EMAIL)
    assert client.get("/api/service-requests/forms").json()["forms"] == []
    gone = client.post("/api/service-requests", json={"category": "facilities", "subject": "Desk", "form_id": form["id"], "answers": {"device_model": "x"}})
    assert gone.status_code == 422 and "no longer available" in gone.json()["error"]["message"]


def test_a_financial_form_still_needs_an_amount_and_follows_department_access(client):
    login(client, ADMIN_EMAIL)
    finance = client.post("/api/admin/access/groups", json={"name": "Finance", "kind": "department"}).json()
    ids = {u["email"]: u["id"] for u in client.get("/api/admin/users").json()["users"]}
    client.patch(f"/api/admin/users/{ids[USER2_EMAIL]}", json={"role": "manager", "department_ids": [finance["id"]]})
    form = client.post("/api/admin/request-forms", json={
        "name": "Travel advance", "category": "expense", "department_group_id": finance["id"],
        "fields": [{"label": "Destination", "type": "text", "required": True}]}).json()["forms"][0]
    login(client, USER_EMAIL)
    payload = {"category": "expense", "subject": "Karachi visit", "form_id": form["id"], "answers": {"destination": "Karachi"}}
    assert client.post("/api/service-requests", json=payload).status_code == 422  # no amount
    made = client.post("/api/service-requests", json={**payload, "amount": "25000", "currency": "PKR"}).json()
    login(client, USER2_EMAIL)  # the Finance manager sees the answers
    seen = next(i for i in client.get("/api/service-requests").json()["items"] if i["id"] == made["id"])
    assert _plain(seen["form_answers"]) == [{"label": "Destination", "value": "Karachi"}] and seen["can_manage"]
