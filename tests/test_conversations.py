"""Persistent conversations: ownership, persistence, bounded context, permissions, provider metadata.
All documents are synthetic."""

from sqlalchemy import text

from app.core.errors import LLMTimeoutError
from app.main import app
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import PROVIDER, activate_provider, make_pdf, process_all, upload

REFUND = "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."
SERVERS = "Server maintenance: the datacenter cooling units are inspected every quarter by the facilities team."


def cited(message: str) -> str:
    return "Customers can request a refund within 30 days [S1]."


def new_chat(client) -> str:
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def send(client, conversation_id: str, question: str):
    return client.post(f"/api/conversations/{conversation_id}/messages", json={"question": question})


def message_rows() -> list[tuple]:
    return [tuple(r) for r in run_db(lambda c: c.execute(text(
        "SELECT role, provider, model, strategy FROM conversation_messages ORDER BY id"))).all()]


def test_conversation_is_saved_and_reopened(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(SERVERS, REFUND))
    process_all(client)
    fake_provider.rag_reply = cited

    chat_id = new_chat(client)
    body = send(client, chat_id, "How many days do customers have to request a refund?").json()
    assert body["conversation"]["title"] == "How many days do customers have to request a refund?"
    assert body["conversation"]["message_count"] == 2
    assert body["assistant_message"]["response"]["citations"][0]["page_number"] == 2

    reopened = client.get(f"/api/conversations/{chat_id}").json()
    assert [m["role"] for m in reopened["messages"]] == ["user", "assistant"]
    answer = reopened["messages"][1]
    assert answer["content"] == "Customers can request a refund within 30 days [S1]."
    assert answer["response"]["citations"][0]["filename"] == "policy.pdf"
    assert answer["response"]["provider"] == "deepseek" and answer["response"]["model"] == "deepseek-flash"
    assert message_rows() == [("user", None, None, None), ("assistant", "deepseek", "deepseek-flash", "semantic")]
    assert [c["id"] for c in client.get("/api/conversations").json()["conversations"]] == [chat_id]


def test_roman_urdu_general_followup_does_not_pick_an_unrelated_document(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    chat_id = new_chat(client)
    fake_provider.rag_reply = lambda _: "Photosynthesis uses light to produce sugars."
    first = send(client, chat_id, "Using general knowledge, explain photosynthesis.").json()
    assert first["assistant_message"]["response"]["general_knowledge"]
    result = send(client, chat_id, "detail btao")
    assert result.status_code == 200
    answer = result.json()["assistant_message"]["response"]
    assert answer["strategy"] == "general" and not answer["insufficient_evidence"]
    assert not answer["citations"]
    _, prompt = fake_provider.calls[-1]
    assert "Photosynthesis uses light" in prompt and REFUND not in prompt


def test_rename_delete_and_cross_user_isolation(client):
    login(client, USER_EMAIL)
    chat_id = new_chat(client)
    assert client.patch(f"/api/conversations/{chat_id}", json={"title": "Budget questions"}).json()["title"] == "Budget questions"

    for email in (USER2_EMAIL, ADMIN_EMAIL):  # admins get no access to users' chats either
        login(client, email)
        assert client.get("/api/conversations").json()["conversations"] == []
        assert client.get(f"/api/conversations/{chat_id}").status_code == 404
        assert client.patch(f"/api/conversations/{chat_id}", json={"title": "x"}).status_code == 404
        assert send(client, chat_id, "hi").status_code == 404
        assert client.delete(f"/api/conversations/{chat_id}").status_code == 404

    login(client, USER_EMAIL)
    assert client.delete(f"/api/conversations/{chat_id}").status_code == 204
    assert client.get(f"/api/conversations/{chat_id}").status_code == 404
    actions = [r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs"))).all()]
    assert "conversation.deleted" in actions
    assert client.post("/api/conversations/x/messages", json={"question": "hi"}).status_code == 422
    client.cookies.clear()
    assert client.get("/api/conversations").status_code == 401


def test_continuing_uses_bounded_server_side_context(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    fake_provider.rag_reply = cited
    chat_id = new_chat(client)
    for n in range(1, 6):
        assert send(client, chat_id, f"refund question number {n}?").status_code == 200

    _, last_prompt = fake_provider.calls[-1]
    conversation = last_prompt.split("SOURCES")[0]
    # Keep the last 3 turns plus a matching earlier turn, without replaying the entire history.
    assert [f"refund question number {n}?" in conversation for n in range(1, 5)] == [True, True, True, True]


def test_follow_up_in_saved_chat_stays_on_the_cited_document(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    sip = upload(client, "sip.pdf", make_pdf("SIP telephony versus mobile SIM report: purpose is a commercial evaluation.")).json()
    upload(client, "it.pdf", make_pdf("IT infrastructure report: purpose is rollout progress and the agenda for the CEO."))
    process_all(client)
    fake_provider.rag_reply = lambda m: "The purpose is a commercial evaluation [S1]."
    chat_id = new_chat(client)
    send(client, chat_id, "what is the SIP telephony report?")
    body = send(client, chat_id, "what is the purpose of this report?").json()
    assert {c["document_id"] for c in body["assistant_message"]["response"]["citations"]} == {sip["id"]}


def test_permissions_are_rechecked_when_reopening_and_continuing(client, fake_provider):
    activate_provider(client)
    shared = upload(client, "policy.pdf", make_pdf(REFUND), visibility="shared").json()  # admin publishes
    process_all(client)
    login(client, USER_EMAIL)
    fake_provider.rag_reply = cited
    chat_id = new_chat(client)
    assert send(client, chat_id, "refund request days?").json()["assistant_message"]["response"]["citations"]

    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/rag/documents/{shared['id']}").status_code == 204
    login(client, USER_EMAIL)

    reopened = client.get(f"/api/conversations/{chat_id}").json()["messages"][1]
    citation = reopened["response"]["citations"][0]
    assert citation["available"] is False and citation["snippet"] == "" and citation["filename"] == "Unavailable document"
    assert reopened["sources_unavailable"] == 1
    assert REFUND[:30] not in client.get(f"/api/conversations/{chat_id}").text

    fake_provider.calls.clear()
    follow_up = send(client, chat_id, "and what about refunds?").json()
    assert follow_up["assistant_message"]["response"]["citations"] == []  # deleted document is never retrieved
    assert all(REFUND[:30] not in message for _, message in fake_provider.calls)


def test_history_survives_a_provider_model_change(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    fake_provider.rag_reply = cited
    chat_id = new_chat(client)
    send(client, chat_id, "refund request days?")

    login(client, ADMIN_EMAIL)
    version = client.get(PROVIDER).json()["version"]
    assert client.put(PROVIDER, json={"model": "deepseek-v4-pro", "settings": {}, "expected_version": version}).status_code == 200
    login(client, USER_EMAIL)
    send(client, chat_id, "refund request days again?")

    models = [m["response"]["model"] for m in client.get(f"/api/conversations/{chat_id}").json()["messages"] if m["response"]]
    assert models == ["deepseek-flash", "deepseek-v4-pro"]  # each answer records what produced it


def test_failed_answers_are_not_saved_and_chats_stay_out_of_the_index(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    chunks_before = run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks")))
    chat_id = new_chat(client)

    fake_provider.chat_error = LLMTimeoutError()
    assert send(client, chat_id, "refund request days?").status_code == 504
    assert message_rows() == []

    fake_provider.chat_error = None
    fake_provider.rag_reply = cited
    send(client, chat_id, "refund request days?")
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks"))) == chunks_before
    db_ids = {r[0] for r in run_db(lambda c: c.execute(text("SELECT id FROM document_chunks"))).all()}
    assert app.state.rag.vector_store.ids() == db_ids  # nothing from the chat was indexed as knowledge


def test_users_pick_a_model_per_chat_without_changing_the_active_provider(client, fake_provider):
    activate_provider(client)  # deepseek / deepseek-flash is the organization's default
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    fake_provider.rag_reply = cited

    options = client.get("/api/rag/models")
    assert [(o["model"], o["is_default"]) for o in options.json()["options"]] == [
        ("deepseek-flash", True), ("deepseek-v4-pro", False)
    ]
    assert "sk-" not in options.text and "https://" not in options.text  # no keys or endpoints

    chat_id = new_chat(client)
    picked = client.post(f"/api/conversations/{chat_id}/messages",
                         json={"question": "refund request days?", "provider": "deepseek", "model": "deepseek-v4-pro"})
    assert picked.json()["assistant_message"]["response"]["model"] == "deepseek-v4-pro"
    assert send(client, chat_id, "refund request days again?").json()["assistant_message"]["response"]["model"] == "deepseek-flash"

    for bad in ({"provider": "deepseek", "model": "gpt-9"}, {"provider": "nope", "model": "x"}, {"provider": "deepseek"}):
        assert client.post(f"/api/conversations/{chat_id}/messages", json={"question": "refund request days?"} | bad).status_code == 422

    login(client, ADMIN_EMAIL)
    state = client.get(PROVIDER).json()
    assert state["model"] == "deepseek-flash" and state["is_active"] is True  # the pick changed nothing globally


def test_a_bare_question_mark_asks_the_unanswered_question_again(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    chat_id = new_chat(client)
    fake_provider.rag_reply = lambda m: "INSUFFICIENT_EVIDENCE: not stated."
    assert send(client, chat_id, "refund request days?").json()["assistant_message"]["response"]["insufficient_evidence"]

    fake_provider.rag_reply = cited
    fake_provider.calls.clear()
    body = send(client, chat_id, "?").json()
    _, prompt = fake_provider.calls[0]
    # The refused turn stays in context, and "?" is answered as that question, not as a cut-off message.
    assert prompt.split("QUESTION:")[-1].strip() == "refund request days?"
    assert "refund request days?" in prompt.split("SOURCES")[0]
    assert body["assistant_message"]["response"]["citations"]
