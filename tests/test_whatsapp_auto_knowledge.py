"""Step AE slice 2 — Knowledge answer path for WhatsApp customers.

Unit tests (monkeypatched retrieval/LLM) prove the gating logic; the
DB-backed tests prove the real retrieval path respects the
customer-visible subset (a document outside the subset is never quoted)
and that the worker entry point returns the Knowledge reply.

Run with the repo conftest (throwaway ``*_test`` database, log capture
on). The DB tests seed approved Knowledge the same way the RAG tests do:
a published shared revision whose evidence points at a ready document
with matching version/sha, readable by the owner.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    Document,
    DocumentChunk,
    KnowledgeEntry,
    KnowledgeEvidence,
    KnowledgeRevision,
    User,
)
from app.db.session import test_engine_options as _engine_options_for_tests
from app.main import app
from app.rag.answer import GroundedAnswer
from app.rag.retrieval import RetrievedChunk
from app.schemas.chat import ChatResponse
from app.whatsapp_auto import knowledge_reply
from app.whatsapp_auto.decider import Evidence, LeadSignals
from app.whatsapp_auto.replies import INSUFFICIENT_EVIDENCE_REPLY
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, login, run_db


def run_session(fn):
    """Run ``await fn(session)`` with a real AsyncSession (``run_db`` only
    gives a Connection, and ``answer_customer_question`` takes an
    AsyncSession — ``db.scalar(select(Entity))`` returns the first column
    on a Connection, not the entity)."""

    async def _run():
        engine = create_async_engine(
            TEST_DATABASE_URL, poolclass=NullPool, **_engine_options_for_tests())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _settings():
    return SimpleNamespace(rag_top_k=3, rag_min_similarity=0.15, rag_max_context_chars=8000)


def _user():
    return User(id=uuid.uuid4(), email="owner@example.com", role="admin", is_active=True)


class _StubLLM:
    """Returns one fixed reply, whatever the prompt is."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        self.calls += 1
        return ChatResponse(reply=self.reply, model="stub", provider="stub")


def _chunk(doc_id: uuid.UUID, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        document_id=doc_id,
        filename="price-list.txt",
        page_number=1,
        chunk_index=0,
        content=content,
        score=0.9,
    )


async def _fake_get_llm(llm):
    return llm


# ---------------------------------------------------------------------------
# Subset parsing + citation stripping (pure)
# ---------------------------------------------------------------------------

def test_subset_parsing_keeps_valid_ids_drops_junk_and_dedupes():
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(whatsapp_customer_visible_document_ids=[
        str(doc_a), "not-a-uuid", str(doc_b), str(doc_a), 12345, None,
    ])
    assert knowledge_reply.customer_visible_document_ids(row) == [doc_a, doc_b]


def test_subset_parsing_empty_for_missing_row_or_column():
    assert knowledge_reply.customer_visible_document_ids(None) == []
    assert knowledge_reply.customer_visible_document_ids(SimpleNamespace()) == []
    assert knowledge_reply.customer_visible_document_ids(
        SimpleNamespace(whatsapp_customer_visible_document_ids=None)) == []


def test_strip_citations_removes_markers_and_collapses_space():
    assert knowledge_reply.strip_citations("Price is PKR 50,000. [S1]") == "Price is PKR 50,000."
    assert knowledge_reply.strip_citations("A [S1] and B. [S2]") == "A and B."
    assert knowledge_reply.strip_citations("No markers here.") == "No markers here."


# ---------------------------------------------------------------------------
# answer_customer_question with mocked retrieval / LLM
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_empty_subset_answers_nothing_and_calls_nothing(monkeypatch):
    calls = {"retrieve": 0}

    async def fake_retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        return []

    monkeypatch.setattr(knowledge_reply._retrieval, "retrieve", fake_retrieve)
    llm = _StubLLM("anything [S1]")
    reply = _run(knowledge_reply.answer_customer_question(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(llm), "price kya hai?",
        document_ids=[],
    ))
    assert reply.found is False
    assert reply.reply_text == INSUFFICIENT_EVIDENCE_REPLY
    assert reply.reason == "no_customer_visible_documents"
    assert calls["retrieve"] == 0
    assert llm.calls == 0


def test_cited_answer_is_returned_with_citations_stripped(monkeypatch):
    doc_id = uuid.uuid4()
    chunk = _chunk(doc_id, "Our standard package costs PKR 50,000 per month.")

    async def fake_retrieve(*args, **kwargs):
        return [chunk]

    async def fake_answer(llm, question, chunks, **kwargs):
        assert kwargs.get("general_answers") is False
        return GroundedAnswer(
            "Our standard package costs PKR 50,000 per month. [S1]",
            False, [(1, chunk)], "stub", "stub",
        )

    monkeypatch.setattr(knowledge_reply._retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(knowledge_reply._answer, "answer_question", fake_answer)
    llm = _StubLLM("unused")
    reply = _run(knowledge_reply.answer_customer_question(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(llm), "standard package kitne ka hai?",
        document_ids=[doc_id],
    ))
    assert reply.found is True
    assert reply.reply_text == "Our standard package costs PKR 50,000 per month."
    assert "[S1]" not in reply.reply_text
    assert reply.citations == (doc_id,)
    assert reply.confidence == 1.0


def test_no_chunks_means_refusal_without_llm_call(monkeypatch):
    async def fake_retrieve(*args, **kwargs):
        return []

    monkeypatch.setattr(knowledge_reply._retrieval, "retrieve", fake_retrieve)
    llm = _StubLLM("anything [S1]")
    reply = _run(knowledge_reply.answer_customer_question(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(llm), "kuch aur?",
        document_ids=[uuid.uuid4()],
    ))
    assert reply.found is False
    assert reply.reason == "no_matching_documents"
    assert llm.calls == 0


@pytest.mark.parametrize("insufficient,general", [(True, False), (False, True)])
def test_insufficient_or_general_answer_is_refused(monkeypatch, insufficient, general):
    doc_id = uuid.uuid4()

    async def fake_retrieve(*args, **kwargs):
        return [_chunk(doc_id, "something")]

    async def fake_answer(llm, question, chunks, **kwargs):
        return GroundedAnswer(
            "A general answer." if general else "INSUFFICIENT_EVIDENCE",
            insufficient, [], "stub", "stub", general=general,
        )

    monkeypatch.setattr(knowledge_reply._retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(knowledge_reply._answer, "answer_question", fake_answer)
    reply = _run(knowledge_reply.answer_customer_question(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(_StubLLM("unused")), "sawal?",
        document_ids=[doc_id],
    ))
    assert reply.found is False
    assert reply.reply_text == INSUFFICIENT_EVIDENCE_REPLY
    assert reply.reason == "insufficient_evidence"


# ---------------------------------------------------------------------------
# Triage (decider + lookup)
# ---------------------------------------------------------------------------

def test_triage_question_with_evidence_returns_knowledge_reply(monkeypatch):
    doc_id = uuid.uuid4()

    async def fake_answer_customer_question(*args, **kwargs):
        return knowledge_reply.KnowledgeReply(
            found=True, reply_text="PKR 50,000.", citations=(doc_id,),
            reason="evidence_backed", confidence=1.0,
        )

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", fake_answer_customer_question)
    decision, knowledge = _run(knowledge_reply.triage_customer_message(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(_StubLLM("unused")),
        "standard package kitne ka hai?", sender_verified=True,
    ))
    assert decision.action == "reply_from_knowledge"
    assert knowledge is not None and knowledge.reply_text == "PKR 50,000."


def test_triage_question_without_evidence_hands_off(monkeypatch):
    async def fake_answer_customer_question(*args, **kwargs):
        return knowledge_reply.KnowledgeReply(
            found=False, reply_text=INSUFFICIENT_EVIDENCE_REPLY,
            reason="insufficient_evidence",
        )

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", fake_answer_customer_question)
    decision, knowledge = _run(knowledge_reply.triage_customer_message(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(_StubLLM("unused")),
        "kya aap ke paas X hai?", sender_verified=True,
    ))
    assert decision.action == "handoff_to_human"
    assert decision.reason == "insufficient_evidence"
    assert knowledge is None


def test_triage_never_looks_up_for_greeting_or_unverified_sender(monkeypatch):
    calls = {"lookup": 0}

    async def fake_answer_customer_question(*args, **kwargs):
        calls["lookup"] += 1
        raise AssertionError("lookup must not run")

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", fake_answer_customer_question)
    decision, _ = _run(knowledge_reply.triage_customer_message(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(_StubLLM("unused")),
        "salam", sender_verified=True,
    ))
    assert decision.action == "reply_canned"
    decision, _ = _run(knowledge_reply.triage_customer_message(
        None, _user(), SimpleNamespace(embedder=None, vector_store=None), _settings(),
        lambda: _fake_get_llm(_StubLLM("unused")),
        "price kya hai?", sender_verified=False,
    ))
    assert decision.action == "handoff_to_human"
    assert decision.reason == "unverified_sender"
    assert calls["lookup"] == 0


# ---------------------------------------------------------------------------
# DB-backed: real retrieval respects the customer-visible subset
# ---------------------------------------------------------------------------

_STATEMENT_A = "Our standard package costs PKR 50000 per month."
_STATEMENT_B = "Our enterprise onboarding takes fourteen business days."
_SHA_A = "a" * 64
_SHA_B = "b" * 64


async def _seed_knowledge(db, owner_id, doc_id, sha, statement):
    """One ready document + draft revision + evidence quote.

    Returns (entry_id, revision_id). The entry is seeded with
    ``current_revision_id=None`` and the revision as a ``draft``: the
    database guards refuse an entry that names a revision that does not
    exist yet, and refuse a revision created as anything but a draft.
    The draft is published through the app API (see ``_publish``) — the
    guards forbid publishing outside the approval workflow. The document
    keeps ``chunk_count=0`` on purpose: retrieval then uses only the
    approved-knowledge path, never the vector store.
    """
    entry_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    quote = statement
    await db.execute(insert(Document).values(
        id=doc_id, owner_id=owner_id, visibility="shared", source_system="upload",
        filename="price-list.txt", file_format="txt", storage_key="test/" + str(doc_id),
        sha256=sha, size_bytes=len(statement), version=1, indexed_version=1,
        indexed_filename="price-list.txt", status="ready", chunk_count=0,
    ))
    await db.execute(insert(KnowledgeEntry).values(
        id=entry_id, author_user_id=owner_id, owner_user_id=owner_id,
        current_revision_id=None,
    ))
    await db.execute(insert(KnowledgeRevision).values(
        id=revision_id, entry_id=entry_id, author_user_id=owner_id,
        revision_number=1, statement=statement, category="general",
        visibility="shared", status="draft",
    ))
    # The evidence guard requires a real chunk row whose content contains
    # the quote at the given offsets. The chunk carries a dummy embedding
    # and the document keeps chunk_count=0, so retrieval still uses only
    # the approved-knowledge path (the vector store never sees it).
    chunk_id = await db.scalar(
        insert(DocumentChunk).values(
            document_id=doc_id, chunk_index=0, page_number=1,
            content=statement, embedding=b"\x00" * 16,
        ).returning(DocumentChunk.id)
    )
    await db.execute(insert(KnowledgeEvidence).values(
        revision_id=revision_id, document_id=doc_id, document_key=doc_id,
        document_version=1, document_sha256=sha, filename="price-list.txt",
        chunk_key=chunk_id, page_number=1, start_offset=0, end_offset=len(quote),
        quote=quote, prefix="", suffix="",
    ))
    return entry_id, revision_id


def _publish(client, entry_id, revision_id):
    """Walk one seeded draft revision to published through the app API.

    The author (admin) submits; a second user reviews and a third
    publishes, each holding the matching grant — the same approval
    workflow the knowledge tests use. Grants are created once per
    fixture run by the caller, not here.
    """
    login(client, ADMIN_EMAIL)
    submitted = client.post(f"/api/knowledge/{entry_id}/submit")
    assert submitted.status_code == 200, submitted.text
    login(client, USER_EMAIL)
    reviewed = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "review", "rationale": "Checked the source text."},
    )
    assert reviewed.status_code == 200, reviewed.text
    login(client, USER2_EMAIL)
    published = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "publish", "rationale": "Approved for shared use."},
    )
    assert published.status_code == 200, published.text


@pytest.fixture()
def seeded_subset(client):
    """Two published documents; only A is customer-visible. Returns their ids.

    B's statement is lexically disjoint from A's on purpose: the hidden
    document's question must share no search term with A's statement,
    or the approved-knowledge path would (correctly, for A) answer from A.
    """
    from app.platform.models import OrgSettings

    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()

    async def seed(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        entry_a, revision_a = await _seed_knowledge(db, owner_id, doc_a, _SHA_A, _STATEMENT_A)
        entry_b, revision_b = await _seed_knowledge(db, owner_id, doc_b, _SHA_B, _STATEMENT_B)
        # No org_settings row exists in a freshly migrated test DB; create
        # one (or update the existing row) so the subset actually lands.
        # A plain scalar() on the settings row would return only its id
        # column on this Connection, so select the id column explicitly.
        existing = await db.scalar(select(OrgSettings.id).where(OrgSettings.id == 1))
        if existing is None:
            await db.execute(insert(OrgSettings).values(
                id=1, whatsapp_customer_visible_document_ids=[str(doc_a)],
            ))
        else:
            await db.execute(update(OrgSettings).where(OrgSettings.id == 1).values(
                whatsapp_customer_visible_document_ids=[str(doc_a)],
            ))
        return owner_id, entry_a, revision_a, entry_b, revision_b

    owner_id, entry_a, revision_a, entry_b, revision_b = run_db(seed)
    # Grants once: the same reviewer/publisher approve both revisions.
    login(client, ADMIN_EMAIL)
    for email, permission in (
        (USER_EMAIL, "knowledge.review"),
        (USER2_EMAIL, "knowledge.publish"),
    ):
        granted = client.post(
            "/api/admin/access/grants",
            json={"email": email, "permission": permission, "category": "general"},
        )
        assert granted.status_code == 201, granted.text
    _publish(client, entry_a, revision_a)
    _publish(client, entry_b, revision_b)
    return {"owner_id": owner_id, "doc_a": doc_a, "doc_b": doc_b}


def _answer_via_app(client, seeded_subset, question):
    from app.core.config import Settings

    async def ask(db):
        user = await db.scalar(select(User).where(User.id == seeded_subset["owner_id"]))
        llm = _StubLLM("Our standard package costs PKR 50000 per month. [S1]")
        return await knowledge_reply.answer_customer_question(
            db, user, app.state.rag, Settings(),
            lambda: _fake_get_llm(llm), question,
        )

    return run_session(ask)


def test_real_retrieval_answers_only_from_visible_document(seeded_subset, client):
    reply = _answer_via_app(
        client, seeded_subset, "standard package kitne ka hai?")
    assert reply.found is True
    assert "50000" in reply.reply_text
    assert "fourteen" not in reply.reply_text
    assert reply.citations == (seeded_subset["doc_a"],)


def test_real_retrieval_refuses_question_about_hidden_document(seeded_subset, client):
    reply = _answer_via_app(
        client, seeded_subset, "enterprise onboarding kitne din mein hoti hai?")
    assert reply.found is False
    assert reply.reply_text == INSUFFICIENT_EVIDENCE_REPLY
    assert seeded_subset["doc_b"] not in reply.citations


def test_real_retrieval_answers_nothing_when_subset_empty(client):
    async def seed(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        await _seed_knowledge(db, owner_id, uuid.uuid4(), _SHA_A, _STATEMENT_A)

    run_db(seed)

    async def ask(db):
        from app.core.config import Settings

        user = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        llm = _StubLLM("should never be called [S1]")
        reply = await knowledge_reply.answer_customer_question(
            db, user, app.state.rag, Settings(),
            lambda: _fake_get_llm(llm), "standard package kitne ka hai?",
        )
        assert reply.found is False
        assert reply.reason == "no_customer_visible_documents"
        assert llm.calls == 0

    run_session(ask)
