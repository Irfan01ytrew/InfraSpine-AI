"""Regression coverage for query-side Urdu expansion and unchanged ACL enforcement."""

from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import User
from app.main import app
from app.rag.evaluation.dataset import load_dataset
from app.rag.query_expansion import expand_multilingual_query
from app.rag.retrieval import retrieve
from tests.conftest import USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import make_pdf, process_all, upload


def test_multilingual_query_expansion_preserves_english_and_adds_english_terms():
    english = "How many working days of annual leave do permanent staff receive?"
    assert expand_multilingual_query(english) == english
    assert expand_multilingual_query("What is the main annual leave policy for staff?") == (
        "What is the main annual leave policy for staff?"
    )

    roman_urdu = expand_multilingual_query("invoice disputes ke liye refund kitne din ka hai?")
    assert {"invoice", "disputes", "refund", "days"} <= set(roman_urdu.split())

    urdu = expand_multilingual_query("انوائس تنازعہ کے لیے رقم واپسی کتنے دن کی ہے؟")
    assert {"invoice", "dispute", "refund", "days"} <= set(urdu.split())

    mixed = expand_multilingual_query("invoice disputes کے لیے refund کتنے days کا ہے؟")
    assert {"invoice", "disputes", "refund", "days"} <= set(mixed.split())

    arabic = expand_multilingual_query("كم عدد أيام استرداد الفاتورة في حالة النزاع؟")
    assert {"days", "refund", "invoice", "dispute"} <= set(arabic.split())


def test_retrieval_queries_keep_the_original_when_the_expansion_loses_the_question():
    from app.rag.query_expansion import retrieval_queries

    english = "How many working days of annual leave do permanent staff receive?"
    assert retrieval_queries(english) == [english]
    # Every Urdu term is known: the English expansion alone (an Urdu original would match Urdu documents at random).
    urdu = "انوائس تنازعہ کے لیے رقم واپسی کتنے دن کی ہے؟"
    assert retrieval_queries(urdu) == [expand_multilingual_query(urdu)]
    # R10 ur-shift: the expansion keeps only "for", so the original question (and its Urdu document) is searched.
    shift = "گودام کا کوڈ کس شفٹ کے لیے ہے؟"
    assert expand_multilingual_query(shift) == "for" and retrieval_queries(shift) == [shift]
    # Partly translated: both, so neither the English documents nor the Urdu ones are lost.
    partial = "گودام کی سالانہ چھٹی کتنے دن ہے؟"
    assert retrieval_queries(partial) == [expand_multilingual_query(partial), partial]


def test_several_queries_share_one_embedding_batch_and_merge_without_duplicates(client, monkeypatch):
    login(client, USER_EMAIL)
    for name, text in (("refunds.pdf", "Billing refund window is thirty days for invoice disputes."),
                       ("network.pdf", "Lahore core switch maintenance runs on Sunday night.")):
        assert upload(client, name, make_pdf(text)).status_code == 202
    process_all(client)
    batches = []
    embed = app.state.rag.embedder.embed
    monkeypatch.setattr(app.state.rag.embedder, "embed", lambda texts: batches.append(list(texts)) or embed(texts))
    settings = get_settings()

    async def search(transform):
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == USER_EMAIL))
            return await retrieve(
                db, user, app.state.rag.embedder, app.state.rag.vector_store, "refund window",
                top_k=5, min_similarity=settings.rag_min_similarity, max_context_chars=settings.rag_max_context_chars,
                query_transform=transform,
            )

    both = client.portal.call(search, lambda q: ["refund window invoice", "Lahore core switch maintenance", q])
    assert len(batches) == 1 and len(batches[0]) == 3  # one embedding batch for all three texts
    names = [chunk.filename for chunk in both]
    assert {"refunds.pdf", "network.pdf"} <= set(names) and len(names) == len(set(names))  # merged, no duplicates

    batches.clear()
    client.portal.call(search, lambda q: q)  # nothing to expand: one text, one search
    assert batches == [["refund window"]]


def test_focused_dataset_covers_all_requested_query_languages():
    dataset = load_dataset(Path("evals/multilingual_retrieval.json"))
    assert {case.language for case in dataset.cases} == {"en", "roman_ur", "ur", "mixed"}
    assert len(dataset.cases) >= 12


def test_multilingual_query_expansion_does_not_bypass_document_permissions(client):
    login(client, USER_EMAIL)
    uploaded = upload(
        client,
        "billing-policy.pdf",
        make_pdf("Billing refund window is thirty days for invoice disputes."),
    )
    assert uploaded.status_code == 202, uploaded.text
    process_all(client)

    question = "انوائس تنازعہ کے لیے رقم واپسی کتنے دن کی ہے؟"
    settings = get_settings()

    async def readable_chunks(email: str):
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            return await retrieve(
                db,
                user,
                app.state.rag.embedder,
                app.state.rag.vector_store,
                question,
                top_k=5,
                min_similarity=settings.rag_min_similarity,
                max_context_chars=settings.rag_max_context_chars,
                query_transform=expand_multilingual_query,
            )

    owner_chunks = client.portal.call(readable_chunks, USER_EMAIL)
    assert any(chunk.filename == "billing-policy.pdf" for chunk in owner_chunks)

    login(client, USER2_EMAIL)
    outsider_chunks = client.portal.call(readable_chunks, USER2_EMAIL)
    assert all(chunk.filename != "billing-policy.pdf" for chunk in outsider_chunks)
