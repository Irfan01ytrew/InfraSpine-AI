"""Conditional form fields: shown only when an earlier choice or yes/no field has a given answer."""

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login

TRAVEL = {
    "name": "Travel request", "category": "other",
    "fields": [
        {"label": "Trip type", "type": "choice", "required": True, "options": ["Local", "International"]},
        {"label": "Passport number", "type": "text", "required": True, "show_if": {"field": "Trip type", "equals": "International"}},
        {"label": "Need a visa letter", "type": "yes_no", "show_if": {"field": "Trip type", "equals": "International"}},
        {"label": "Embassy city", "type": "text", "required": True, "show_if": {"field": "need_a_visa_letter", "equals": "Yes"}},
        {"label": "Notes", "type": "text"},
    ],
}


def _submit(client, form_id, answers):
    return client.post("/api/service-requests", json={"category": "other", "subject": "Trip", "form_id": form_id, "answers": answers})


def test_hidden_fields_are_not_required_and_their_answers_are_dropped(client):
    login(client, ADMIN_EMAIL)
    created = client.post("/api/admin/request-forms", json=TRAVEL)
    assert created.status_code == 201, created.text
    fields = created.json()["forms"][0]["fields"]
    assert fields[1]["show_if"] == {"field": "trip_type", "equals": "International"}
    assert fields[3]["show_if"] == {"field": "need_a_visa_letter", "equals": "Yes"}
    form_id = created.json()["forms"][0]["id"]

    login(client, USER_EMAIL)
    local = _submit(client, form_id, {"trip_type": "Local", "passport_number": "AB123", "embassy_city": "X", "notes": "Lahore"})
    assert local.status_code == 201, local.text  # passport not required, and not kept
    assert [a["label"] for a in local.json()["form_answers"]] == ["Trip type", "Notes"]

    assert _submit(client, form_id, {"trip_type": "International"}).status_code == 422  # passport now required
    no_visa = _submit(client, form_id, {"trip_type": "International", "passport_number": "AB123", "need_a_visa_letter": False})
    assert no_visa.status_code == 201  # embassy city hidden because no visa letter
    assert [a["value"] for a in no_visa.json()["form_answers"]] == ["International", "AB123", "No"]
    assert _submit(client, form_id, {"trip_type": "International", "passport_number": "AB123", "need_a_visa_letter": True}).status_code == 422
    full = _submit(client, form_id, {"trip_type": "International", "passport_number": "AB123", "need_a_visa_letter": True, "embassy_city": "Islamabad"})
    assert [a["label"] for a in full.json()["form_answers"]] == ["Trip type", "Passport number", "Need a visa letter", "Embassy city"]


def test_conditions_must_point_to_an_earlier_choice_or_yes_no_answer(client):
    login(client, ADMIN_EMAIL)
    def make(fields):
        return client.post("/api/admin/request-forms", json={"name": "Bad", "category": "other", "fields": fields})
    later = make([{"label": "A", "type": "text", "show_if": {"field": "B", "equals": "Yes"}}, {"label": "B", "type": "yes_no"}])
    assert later.status_code == 422 and "above it" in later.json()["error"]["message"]
    text_parent = make([{"label": "A", "type": "text"}, {"label": "B", "type": "text", "show_if": {"field": "a", "equals": "x"}}])
    assert text_parent.status_code == 422 and "choice or yes/no" in text_parent.json()["error"]["message"]
    bad_value = make([{"label": "A", "type": "choice", "options": ["One", "Two"]}, {"label": "B", "type": "text", "show_if": {"field": "a", "equals": "Three"}}])
    assert bad_value.status_code == 422
