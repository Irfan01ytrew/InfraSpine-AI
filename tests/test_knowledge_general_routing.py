"""General requests must not inherit incidental library matches or old document context."""
from tests.conftest import USER_EMAIL, login
from tests.rag_helpers import activate_provider, make_pdf, ready


def test_explicit_general_code_and_table_request_skips_document_evidence(client, fake_provider):
    activate_provider(client)
    ready(client, "knowledge-table.pdf", make_pdf("General knowledge company table: refunds are accepted within 30 days."), visibility="shared")
    login(client, USER_EMAIL)
    conversation = client.post("/api/conversations", json={}).json()["id"]
    fake_provider.rag_reply = lambda _: "```python\ndef add(a, b):\n    return a + b\n```\n\n| Input | Result |\n| --- | --- |\n| 2 + 3 | 5 |"
    response = client.post(f"/api/conversations/{conversation}/messages", json={
        "question": "Using general knowledge, show a Python addition function and a comparison table."
    })
    assert response.status_code == 200
    answer = response.json()["assistant_message"]["response"]
    assert answer["general_knowledge"] and answer["strategy"] == "general"
    assert not answer["insufficient_evidence"] and not answer["citations"]
    assert answer["retrieved_chunks"] == 0
    assert "def add" in answer["answer"]
    assert "30 days" not in fake_provider.calls[-1][1]


def test_general_topic_switch_clears_document_scope_for_next_followup(client, fake_provider):
    activate_provider(client)
    ready(client, "refund-policy.pdf", make_pdf("Refund requests are accepted within 30 days through billing support."), visibility="shared")
    login(client, USER_EMAIL)
    conversation = client.post("/api/conversations", json={}).json()["id"]
    route = f"/api/conversations/{conversation}/messages"
    fake_provider.rag_reply = lambda _: "Refund requests are accepted within 30 days [S1]."
    first = client.post(route, json={"question": "How many days do I have for a refund in refund-policy.pdf?"}).json()
    assert first["assistant_message"]["response"]["citations"]
    fake_provider.rag_reply = lambda _: "Photosynthesis uses sunlight to produce sugar."
    second = client.post(route, json={"question": "Switch topics. Using general knowledge, explain photosynthesis."}).json()
    assert second["assistant_message"]["response"]["general_knowledge"]
    assert "30 days" not in fake_provider.calls[-1][1]
    fake_provider.rag_reply = lambda _: "Photosynthesis releases oxygen."
    third = client.post(route, json={"question": "What gas does that process release?"}).json()
    assert third["assistant_message"]["response"]["general_knowledge"]
    assert not third["assistant_message"]["response"]["citations"]
