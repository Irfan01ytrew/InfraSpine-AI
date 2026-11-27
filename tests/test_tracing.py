"""Per-question traces: what they record, what they never record, failures, feedback and the admin view."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.errors import LLMTimeoutError
from app.main import app
from tests.conftest import CSRF, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all, upload

REFUND = "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."
QUESTION = "How many refund request days do customers get?"


def _cited(message: str) -> str:
    return "Customers can request a refund within 30 days [S1]."


def _traces() -> list[dict]:
    rows = run_db(lambda c: c.execute(text("SELECT * FROM rag_traces ORDER BY id")))
    return [dict(row._mapping) for row in rows]


def _chat(client) -> str:
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    return client.post("/api/conversations", json={}).json()["id"]


def test_an_answer_is_traced_with_its_evidence_but_without_any_text(client, fake_provider):
    chat_id = _chat(client)
    fake_provider.rag_reply = _cited
    body = client.post(f"/api/conversations/{chat_id}/messages", json={"question": QUESTION}).json()

    [trace] = _traces()
    assert trace["status"] == "ok" and trace["route"] == "conversation" and trace["decision"] == "supported"
    assert trace["message_id"] == body["assistant_message"]["id"]
    assert trace["question_chars"] == len(QUESTION) and trace["llm_calls"] >= 1
    assert {"retrieval", "llm", "answer"} <= set(trace["timings"])
    assert trace["retrieved"] and trace["retrieved"][0]["cited"] is True
    stored = json.dumps(trace, default=str)
    assert QUESTION not in stored and "refund" not in stored.lower()  # no question, answer or passage text


def test_a_failed_question_keeps_its_error_type_and_stack(client, fake_provider):
    chat_id = _chat(client)
    fake_provider.chat_error = LLMTimeoutError()
    assert client.post(f"/api/conversations/{chat_id}/messages", json={"question": QUESTION}).status_code == 504

    [trace] = _traces()
    assert trace["status"] == "error" and trace["error_type"].endswith("LLMTimeoutError")
    assert "answer_question" in trace["error_stack"] and trace["message_id"] is None


def test_a_failed_model_call_is_attributed_to_its_provider_in_usage_reports(client, fake_provider):
    chat_id = _chat(client)
    fake_provider.chat_error = LLMTimeoutError()
    assert client.post(f"/api/conversations/{chat_id}/messages", json={"question": QUESTION}).status_code == 504

    [trace] = _traces()
    assert trace["status"] == "error" and trace["llm_calls"] >= 1 and trace["provider"] == "deepseek"
    login(client)
    report = client.get("/api/admin/reports/usage?days=7").json()
    assert [(row["provider"], row["calls"]) for row in report["providers"]] == [("deepseek", trace["llm_calls"])]


def test_an_unexpected_error_returns_its_request_id_and_hides_the_message(fake_provider):
    with TestClient(app, headers=CSRF, raise_server_exceptions=False) as client:
        chat_id = _chat(client)
        fake_provider.chat_error = RuntimeError("private words from the prompt")
        response = client.post(f"/api/conversations/{chat_id}/messages", json={"question": QUESTION})

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["message"] == "An unexpected error occurred." and error["request_id"] == response.headers["X-Request-ID"]
    [trace] = _traces()
    assert trace["request_id"] == error["request_id"] and trace["error_type"] == "builtins.RuntimeError"
    assert "private words" not in (trace["error_stack"] or "")


def test_feedback_is_owner_only_and_shows_in_the_admin_view(client, fake_provider):
    chat_id = _chat(client)
    fake_provider.rag_reply = _cited
    message_id = client.post(f"/api/conversations/{chat_id}/messages", json={"question": QUESTION}).json()[
        "assistant_message"
    ]["id"]
    url = f"/api/conversations/{chat_id}/messages/{message_id}/feedback"
    assert client.put(url, json={"rating": -1}).json()["feedback"] == -1
    assert client.get(f"/api/conversations/{chat_id}").json()["messages"][1]["feedback"] == -1
    assert client.get("/api/admin/traces").status_code == 403

    login(client, USER2_EMAIL)
    assert client.put(url, json={"rating": 1}).status_code == 404

    login(client)
    listing = client.get("/api/admin/traces").json()
    assert listing["summary"]["questions"] == 1 and listing["summary"]["not_helpful"] == 1
    assert listing["summary"]["decisions"] == [{"decision": "supported", "count": 1}]
    [daily] = listing["summary"]["daily"]
    assert daily["questions"] == 1 and daily["errors"] == daily["refused"] == daily["citation_refusals"] == 0
    [row] = listing["traces"]
    assert row["feedback"] == -1 and row["user_email"] == USER_EMAIL and row["cited_count"] == 1
    detail = client.get(f"/api/admin/traces/{row['id']}").json()
    assert detail["retrieved"][0]["filename"] == "policy.pdf"
    assert client.get("/api/admin/traces", params={"outcome": "refused"}).json()["traces"] == []
