"""Step U slice 3: server-side Idempotency-Key dedupe audit, end to end.

Every queueable kind maps to a real endpoint here, and each one must honour
``Idempotency-Key``: a replay returns the original result without executing
again, the same key with a different request is a 409, and the replay endpoint
exposes the stored result. Runs against a throwaway *_test database.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.session import test_engine_options
from app.platform.idempotency import canonical_request_hash, key_digest, prune_records
from app.platform.models import IdempotencyRecord
from app.schemas.conversations import SendMessageRequest
from app.schemas.rag import RagChatRequest
from tests.conftest import TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, ready

REFUND = "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."
KEY = "offline-replay-key-0001"


def cited(message: str) -> str:
    return "Customers can request a refund within 30 days [S1]."


def user_id(email: str) -> str:
    rows = run_db(lambda c: c.execute(text("SELECT id FROM users WHERE email=:e"), {"e": email})).all()
    return str(rows[0][0])


def rag_body(doc_id: str, question: str = "How many days do customers have to request a refund?") -> dict:
    return {"question": question, "focus_document_ids": [doc_id]}


def ask_rag(client, doc_id: str, key: str | None = None, **overrides):
    headers = {"Idempotency-Key": key} if key is not None else {}
    body = rag_body(doc_id, **overrides)
    return client.post("/api/rag/chat", json=body, headers=headers)


def setup_doc(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = ready(client, "policy.pdf", make_pdf(REFUND))
    fake_provider.rag_reply = cited
    return doc


def new_chat(client) -> str:
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def send(client, conversation_id: str, question: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(f"/api/conversations/{conversation_id}/messages", json={"question": question}, headers=headers)


def message_count() -> int:
    return run_db(lambda c: c.execute(text("SELECT count(*) FROM conversation_messages"))).scalar_one()


def insert_record(user_email: str, key: str, endpoint: str, request_hash: str, status: str, age: timedelta):
    uid = user_id(user_email)

    async def _insert(conn):
        await conn.execute(
            text(
                "INSERT INTO idempotency_records (id, user_id, key_digest, endpoint, request_hash, status, created_at)"
                " VALUES (:id, :user_id, :digest, :endpoint, :hash, :status, :created)"
            ),
            {
                "id": uuid4(),
                "user_id": uid,
                "digest": key_digest(key),
                "endpoint": endpoint,
                "hash": request_hash,
                "status": status,
                "created": datetime.now(UTC) - age,
            },
        )

    run_db(_insert)


def rag_hash(doc_id: str, question: str = "How many days do customers have to request a refund?") -> str:
    payload = RagChatRequest(question=question, focus_document_ids=[doc_id])
    return canonical_request_hash({"endpoint": "rag_chat", **payload.model_dump(mode="json")})


# --- POST /api/rag/chat (knowledge-question) ---


def test_rag_chat_replay_returns_original_without_second_llm_call(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    first = ask_rag(client, doc["id"], key=KEY)
    assert first.status_code == 200, first.text
    assert len(fake_provider.calls) == 1

    replay = ask_rag(client, doc["id"], key=KEY)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(fake_provider.calls) == 1  # no second model call


def test_rag_chat_same_key_different_request_is_conflict(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    assert ask_rag(client, doc["id"], key=KEY).status_code == 200
    other = ask_rag(client, doc["id"], key=KEY, question="What is the server maintenance schedule?")
    assert other.status_code == 409, other.text
    assert len(fake_provider.calls) == 1


def test_rag_chat_different_key_executes_again(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    assert ask_rag(client, doc["id"], key=KEY).status_code == 200
    assert ask_rag(client, doc["id"], key="offline-replay-key-0002").status_code == 200
    assert len(fake_provider.calls) == 2


def test_rag_chat_without_key_executes_every_time(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    assert ask_rag(client, doc["id"]).status_code == 200
    assert ask_rag(client, doc["id"]).status_code == 200
    assert len(fake_provider.calls) == 2


def test_rag_chat_fresh_pending_key_gets_429_and_stale_pending_is_reclaimed(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    digest_hash = rag_hash(doc["id"])
    insert_record(USER_EMAIL, KEY, "rag_chat", digest_hash, "pending", timedelta(minutes=1))
    assert ask_rag(client, doc["id"], key=KEY).status_code == 429
    assert len(fake_provider.calls) == 0

    insert_record(USER_EMAIL, "offline-replay-key-0003", "rag_chat", digest_hash, "pending", timedelta(minutes=11))
    response = ask_rag(client, doc["id"], key="offline-replay-key-0003")
    assert response.status_code == 200, response.text
    assert len(fake_provider.calls) == 1


# --- POST /api/conversations/{id}/messages (knowledge-question) ---


def test_conversation_message_replay_creates_no_duplicate_messages(client, fake_provider):
    setup_doc(client, fake_provider)
    chat_id = new_chat(client)
    question = "How many days do customers have to request a refund?"
    first = send(client, chat_id, question, key=KEY)
    assert first.status_code == 200, first.text
    assert message_count() == 2
    assert len(fake_provider.calls) == 1

    replay = send(client, chat_id, question, key=KEY)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert message_count() == 2  # no duplicate user/assistant pair
    assert len(fake_provider.calls) == 1


def test_conversation_message_same_key_different_question_is_conflict(client, fake_provider):
    setup_doc(client, fake_provider)
    chat_id = new_chat(client)
    assert send(client, chat_id, "How many days do customers have to request a refund?", key=KEY).status_code == 200
    other = send(client, chat_id, "What is the server maintenance schedule?", key=KEY)
    assert other.status_code == 409, other.text
    assert message_count() == 2


# --- GET /api/offline/replay/{key} ---


def test_replay_endpoint_returns_record_and_404s_for_unknown_and_other_users_keys(client, fake_provider):
    doc = setup_doc(client, fake_provider)
    first = ask_rag(client, doc["id"], key=KEY)
    assert first.status_code == 200

    record = client.get(f"/api/offline/replay/{KEY}")
    assert record.status_code == 200, record.text
    body = record.json()
    assert body["endpoint"] == "rag_chat"
    assert body["status"] == "completed"
    assert body["response_status"] == 200
    assert body["response_body"] == first.json()

    assert client.get("/api/offline/replay/no-such-key").status_code == 404

    # Another user cannot read the first user's record, and the same key is fresh for them.
    login(client, USER2_EMAIL)
    assert client.get(f"/api/offline/replay/{KEY}").status_code == 404
    other = client.post("/api/rag/chat", json={"question": "Is there a refund policy?"}, headers={"Idempotency-Key": KEY})
    assert other.status_code == 200, other.text
    assert other.json() != first.json()
    own = client.get(f"/api/offline/replay/{KEY}")
    assert own.status_code == 200
    assert own.json()["response_body"] == other.json()


def test_replay_endpoint_requires_sign_in(client):
    assert client.get(f"/api/offline/replay/{KEY}").status_code == 401


# --- Workflow draft-save endpoints ---


def setup_scope(client):
    from tests.test_workflow_engine import setup_scope as _setup_scope

    return _setup_scope(client)


def spec():
    from tests.test_workflow_engine import spec as _spec

    return _spec()


def workflow_count(name: str) -> int:
    return run_db(lambda c: c.execute(
        text("SELECT count(*) FROM wf_definitions WHERE name=:n"), {"n": name})).scalar_one()


def test_workflow_draft_save_replay_returns_original_draft(client):
    scope, _people = setup_scope(client)
    body = {"name": "Draft workflow", "spec": spec()}
    first = client.post(f"/api/workflow-engine/scopes/{scope}/workflows", json=body, headers={"Idempotency-Key": KEY})
    assert first.status_code == 201, first.text
    assert workflow_count("Draft workflow") == 1

    replay = client.post(f"/api/workflow-engine/scopes/{scope}/workflows", json=body, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert workflow_count("Draft workflow") == 1  # no second workflow


def test_workflow_save_version_replay_returns_original_version(client):
    scope, _people = setup_scope(client)
    created = client.post(f"/api/workflow-engine/scopes/{scope}/workflows",
                          json={"name": "Draft workflow", "spec": spec()})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["id"]
    body = {"name": "Draft workflow", "spec": spec(), "base_version": 1}
    first = client.post(f"/api/workflow-engine/scopes/{scope}/workflows/{workflow_id}/versions",
                        json=body, headers={"Idempotency-Key": KEY})
    assert first.status_code == 201, first.text

    replay = client.post(f"/api/workflow-engine/scopes/{scope}/workflows/{workflow_id}/versions",
                         json=body, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    versions = run_db(lambda c: c.execute(
        text("SELECT count(*) FROM wf_versions WHERE workflow_id=:w"), {"w": workflow_id})).scalar_one()
    assert versions == 2  # version 1 + version 2, not a third


def test_draft_from_text_replay_skips_second_model_call(client, fake_provider):
    scope, _people = setup_scope(client)
    activate_provider(client)
    login(client, USER_EMAIL)
    fake_provider.rag_reply = lambda message: json.dumps(spec())
    body = {"requirements": "A three step approval workflow for new starters."}
    first = client.post(f"/api/workflow-engine/scopes/{scope}/draft-from-text",
                        json=body, headers={"Idempotency-Key": KEY})
    assert first.status_code == 200, first.text
    assert first.json()["requires_review"] is True
    assert len(fake_provider.calls) == 1

    replay = client.post(f"/api/workflow-engine/scopes/{scope}/draft-from-text",
                         json=body, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(fake_provider.calls) == 1


# --- Ledger maintenance ---


def test_prune_records_removes_only_old_completed_rows(client):
    me = user_id(USER_EMAIL)

    async def _seed(session: AsyncSession):
        now = datetime.now(UTC)
        session.add_all([
            IdempotencyRecord(user_id=me, key_digest=key_digest("old-completed"), endpoint="rag_chat",
                              request_hash="a" * 64, status="completed", created_at=now - timedelta(days=100)),
            IdempotencyRecord(user_id=me, key_digest=key_digest("fresh-completed"), endpoint="rag_chat",
                              request_hash="b" * 64, status="completed", created_at=now - timedelta(days=1)),
            IdempotencyRecord(user_id=me, key_digest=key_digest("old-pending"), endpoint="rag_chat",
                              request_hash="c" * 64, status="pending", created_at=now - timedelta(days=100)),
        ])
        await session.commit()
        pruned = await prune_records(session)
        remaining = (await session.execute(text("SELECT key_digest FROM idempotency_records"))).scalars().all()
        return pruned, set(remaining)

    async def _run():
        engine = create_async_engine(TEST_DATABASE_URL, **test_engine_options())
        try:
            async with AsyncSession(engine) as session:
                return await _seed(session)
        finally:
            await engine.dispose()

    import asyncio

    pruned, remaining = asyncio.run(_run())
    assert pruned == 1
    assert remaining == {key_digest("fresh-completed"), key_digest("old-pending")}
