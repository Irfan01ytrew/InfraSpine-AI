"""ADR-004 WP2: personal memory. Ownership and privacy, chat capture (remember / update / forget / list),
supersession and de-duplication, bounded prompt context, separation from organizational knowledge,
and persistence across restarts. Runs against real PostgreSQL; all content is synthetic."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.main import app
from app.services.audit import AuditContext
from app.services.memory import MemoryService, parse_memory_command
from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all, upload

REFUND = "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."


def remember(client, content: str, **extra):
    return client.post("/api/memory", json={"content": content} | extra)


def memories(client, include_superseded: bool = False) -> list[dict]:
    suffix = "?include_superseded=true" if include_superseded else ""
    return client.get(f"/api/memory{suffix}").json()["items"]


def chat(client, chat_id: str, question: str) -> dict:
    response = client.post(f"/api/conversations/{chat_id}/messages", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()["assistant_message"]


def prompts(fake_provider) -> str:
    return "\n".join(message for _, message in fake_provider.calls)


def test_explicit_commands_are_recognised_and_questions_are_not():
    assert parse_memory_command("Remember that I prefer answers in bullet points").text == "I prefer answers in bullet points"
    assert parse_memory_command("please remember: my team is Finance").text == "my team is Finance"
    assert parse_memory_command("update my memory: my team is Audit").text == "my team is Audit"
    assert parse_memory_command("meri report ki language Urdu hai, yaad rakho").text == "meri report ki language Urdu hai"
    assert parse_memory_command("From now on, answer in Urdu").text == "From now on, answer in Urdu"
    assert (parse_memory_command("forget my team").action, parse_memory_command("forget my team").text) == ("forget", "my team")
    assert parse_memory_command("What do you remember about me?").action == "list"
    for question in ("Remember the refund policy we discussed?", "What is the refund policy?", "Do you remember me?", "hi"):
        assert parse_memory_command(question) is None


def test_memory_api_supersedes_deduplicates_validates_and_deletes(client):
    login(client, USER_EMAIL)
    first = remember(client, "My preferred currency is PKR")
    assert first.status_code == 201 and first.json()["outcome"] == "created"
    assert first.json()["item"]["subject"] == "preferred currency" and first.json()["item"]["kind"] == "fact"
    assert remember(client, "my preferred currency is pkr.").json()["outcome"] == "duplicate"  # normalised duplicate

    second = remember(client, "My preferred currency is USD").json()  # same subject: supersedes, no conflict
    assert second["outcome"] == "superseded" and second["replaced_id"] == first.json()["item"]["id"]
    assert [m["content"] for m in memories(client)] == ["My preferred currency is USD"]
    assert len(memories(client, include_superseded=True)) == 2

    pref = remember(client, "I prefer short answers with tables").json()["item"]
    assert pref["kind"] == "preference" and pref["subject"] is None
    edited = client.patch(f"/api/memory/{pref['id']}", json={"content": "I prefer short answers with bullet points"})
    assert edited.status_code == 200 and edited.json()["outcome"] == "updated"
    assert client.patch(f"/api/memory/{pref['id']}", json={"kind": "fact"}).status_code == 409  # old version is history
    clash = client.patch(f"/api/memory/{edited.json()['item']['id']}", json={"subject": "preferred currency"})
    assert clash.status_code == 409  # would conflict with the active currency memory

    assert remember(client, "x" * 501).status_code == 422
    assert remember(client, "   ").status_code == 422
    assert remember(client, "ok", kind="secret").status_code == 422

    # Deleting forgets the memory together with its earlier versions.
    assert client.delete(f"/api/memory/{second['item']['id']}").status_code == 204
    assert [m["content"] for m in memories(client, include_superseded=True)] == [
        "I prefer short answers with bullet points", "I prefer short answers with tables"
    ]
    assert client.delete("/api/memory").status_code == 422  # wiping everything needs ?all=true
    assert client.delete("/api/memory?all=true").json() == {"deleted": 2}
    assert memories(client, include_superseded=True) == []
    actions = [r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'memory.%'"))).all()]
    assert {"memory.created", "memory.superseded", "memory.updated", "memory.deleted", "memory.cleared"} <= set(actions)
    details = run_db(lambda c: c.execute(text("SELECT details::text FROM audit_logs WHERE action LIKE 'memory.%'"))).all()
    assert all("PKR" not in d[0] and "bullet" not in d[0] for d in details)  # audit never stores memory content


def test_memories_are_private_to_their_owner(client):
    login(client, USER_EMAIL)
    item = remember(client, "My manager is Ayesha").json()["item"]
    for email in (USER2_EMAIL, ADMIN_EMAIL):  # admins get no access either
        login(client, email)
        assert memories(client, include_superseded=True) == []
        assert client.patch(f"/api/memory/{item['id']}", json={"content": "hijacked"}).status_code == 404
        assert client.delete(f"/api/memory/{item['id']}").status_code == 404
        assert client.delete("/api/memory?all=true").json() == {"deleted": 0}
        # The same subject for another user is independent (no cross-user conflict).
        assert remember(client, "My manager is Bilal").json()["outcome"] == "created"
    login(client, USER_EMAIL)
    assert [m["content"] for m in memories(client)] == ["My manager is Ayesha"]
    client.cookies.clear()
    assert client.get("/api/memory").status_code == 401


def test_chat_commands_remember_update_forget_and_list(client, fake_provider):
    login(client, USER_EMAIL)
    chat_id = client.post("/api/conversations", json={}).json()["id"]

    saved = chat(client, chat_id, "Remember that my preferred currency is PKR")
    assert saved["response"]["strategy"] == "memory" and "PKR" in saved["content"]
    assert chat(client, chat_id, "remember my preferred currency is PKR")["content"].startswith("I already remember")
    assert "instead of" in chat(client, chat_id, "update my memory: my preferred currency is USD")["content"]
    chat(client, chat_id, "yaad rakho ke I prefer answers in Urdu")
    assert [m["content"] for m in memories(client)] == ["I prefer answers in Urdu", "my preferred currency is USD"]
    assert memories(client)[1]["source"] == "chat"

    listed = chat(client, chat_id, "what do you remember about me?")["content"]
    assert "USD" in listed and "Urdu" in listed and "PKR" not in listed
    assert "forgotten" in chat(client, chat_id, "forget my preferred currency")["content"]
    assert "matching" in chat(client, chat_id, "forget my favourite colour")["content"]
    assert [m["content"] for m in memories(client)] == ["I prefer answers in Urdu"]
    assert fake_provider.calls == []  # memory commands never call the LLM


def test_memories_shape_answers_but_stay_out_of_knowledge(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    chunks_before = run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks")))
    remember(client, "I prefer answers as a numbered list")
    remember(client, "By ticket I mean a billing support case")
    fake_provider.rag_reply = lambda message: "1. Customers can request a refund within 30 days [S1]."

    chat_id = client.post("/api/conversations", json={}).json()["id"]
    answer = chat(client, chat_id, "How many days do customers have to request a refund?")
    prompt = prompts(fake_provider)
    assert "USER MEMORY" in prompt and "numbered list" in prompt and "billing support case" in prompt
    assert prompt.index("USER MEMORY") < prompt.index("SOURCES")
    assert answer["response"]["memories_applied"] == 2
    assert [c["filename"] for c in answer["response"]["citations"]] == ["policy.pdf"]  # memory is never a source
    reopened = client.get(f"/api/conversations/{chat_id}").json()["messages"][1]
    assert reopened["response"]["memories_applied"] == 2  # persisted with the answer

    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM document_chunks"))) == chunks_before
    db_ids = {r[0] for r in run_db(lambda c: c.execute(text("SELECT id FROM document_chunks"))).all()}
    assert app.state.rag.vector_store.ids() == db_ids  # nothing from memory reached the knowledge index

    # Another user asking the same question gets none of this user's memories.
    login(client, ADMIN_EMAIL)
    upload(client, "shared.pdf", make_pdf(REFUND), visibility="shared")
    process_all(client)
    login(client, USER2_EMAIL)
    fake_provider.calls.clear()
    other = client.post("/api/rag/chat", json={"question": "How many days do customers have to request a refund?"})
    assert other.json()["memories_applied"] == 0
    assert "USER MEMORY" not in prompts(fake_provider) and "numbered list" not in prompts(fake_provider)


def test_forgotten_memories_do_not_linger_in_chat_context(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "policy.pdf", make_pdf(REFUND))
    process_all(client)
    fake_provider.rag_reply = lambda message: "Customers can request a refund within 30 days [S1]."
    chat_id = client.post("/api/conversations", json={}).json()["id"]
    chat(client, chat_id, "remember that my nickname is Falcon")
    chat(client, chat_id, "forget my nickname")
    fake_provider.calls.clear()
    chat(client, chat_id, "How many days for a refund request?")
    assert "Falcon" not in prompts(fake_provider)  # neither in USER MEMORY nor in the conversation history


def test_context_is_bounded_and_prefers_preferences_then_relevance(client):
    login(client, USER_EMAIL)
    remember(client, "I prefer concise answers")
    for i in range(12):
        remember(client, f"My project number {i} codename is Orion{i} with filler text to use up the budget quickly")
    remember(client, "My refund approver is the billing support lead")
    tight = get_settings().model_copy(update={"memory_context_chars": 300, "memory_context_items": 4})

    async def _context(db):
        user_id = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": USER_EMAIL})
        service = MemoryService(db, tight, SimpleNamespace(id=user_id), AuditContext(None, None, None, None))
        return await service.context("Who approves a refund request?", app.state.rag.embedder)

    async def run(fn):
        async with app.state.sessionmaker() as session:
            return await fn(session)

    selected = client.portal.call(run, _context)
    assert len(selected) <= 4 and sum(map(len, selected)) <= 300
    assert selected[0] == "I prefer concise answers"  # preferences always come first
    assert "My refund approver is the billing support lead" in selected  # then the most relevant facts


def test_database_rejects_duplicate_or_conflicting_active_memories(client):
    login(client, USER_EMAIL)
    item = remember(client, "My team is Finance").json()["item"]
    columns = "owner_user_id, kind, subject, subject_key, content, content_key, source"

    def clone(subject_key: str, content_key: str):
        return run_db(lambda c: c.execute(text(
            f"INSERT INTO memory_items (id, {columns}) SELECT gen_random_uuid(), owner_user_id, kind, subject, "
            f"{subject_key}, content, {content_key}, source FROM memory_items WHERE id = :id"
        ), {"id": item["id"]}))

    with pytest.raises(Exception, match="uq_memory_items_active_subject"):
        clone("subject_key", "md5(random()::text) || md5(random()::text)")  # same subject, other content
    with pytest.raises(Exception, match="uq_memory_items_active_content"):
        clone("'other subject'", "content_key")  # same content, other subject
    with pytest.raises(Exception, match="content_length"):
        run_db(lambda c: c.execute(text("UPDATE memory_items SET content = ' '")))


def test_memory_survives_an_application_restart():
    with TestClient(app, headers=CSRF) as first:
        login(first, USER_EMAIL)
        assert remember(first, "My office is in Lahore").status_code == 201
    with TestClient(app, headers=CSRF) as second:  # a fresh lifespan: new engine, new runtime state
        login(second, USER_EMAIL)
        assert [m["content"] for m in memories(second)] == ["My office is in Lahore"]
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM memory_items"))) == 1
