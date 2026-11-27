"""End-to-end RAG tests: real PostgreSQL + real FAISS index, fake embedder and fake LLM.
All documents are synthetic."""

import json
import logging
from functools import partial

from sqlalchemy import text

from app.core.errors import LLMTimeoutError
from app.main import app
from app.rag.answer import INSUFFICIENT_MARKER, UNGROUNDED_ANSWER, UNVERIFIED_FACT
from tests.conftest import (
    ADMIN_EMAIL,
    CSRF,
    RAG_TEST_DIR,
    USER2_EMAIL,
    USER_EMAIL,
    TestClient,
    login,
    run_db,
)
from tests.rag_helpers import (
    activate_provider,
    ask,
    document,
    make_encrypted_pdf,
    make_pdf,
    process_all,
    process_one,
    replace_file,
    upload,
    vector_ids,
)

REFUND_PAGE = "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."
SERVER_PAGE = "Server maintenance: the datacenter cooling units are inspected every quarter by the facilities team."
ALPHA_SECRET = "Project Falcon acquisition target valuation is confidential and known only to the strategy team."


def db_chunk_ids(document_id: str | None = None) -> set[int]:
    query = "SELECT id FROM document_chunks" + (" WHERE document_id = :d" if document_id else "")
    rows = run_db(lambda c: c.execute(text(query), {"d": document_id} if document_id else {}))
    return {r[0] for r in rows.all()}


def uploaded_files() -> set[str]:
    folder = RAG_TEST_DIR / "uploads"
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def cited_reply(message: str) -> str:
    return "Customers can request a refund within 30 days [S1]."


# ---------------------------------------------------------------- ingestion


def test_upload_processes_into_indexed_chunks(client):
    login(client, USER_EMAIL)
    response = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE, SERVER_PAGE))
    assert response.status_code == 202
    doc = response.json()
    assert doc["status"] == "pending" and doc["searchable"] is False and doc["page_count"] == 2

    assert process_all(client) == 1
    doc = document(client, doc["id"])
    assert doc["status"] == "ready" and doc["searchable"] and doc["chunk_count"] >= 2 and doc["indexed_version"] == 1

    pages = run_db(lambda c: c.execute(text("SELECT DISTINCT page_number FROM document_chunks ORDER BY 1"))).all()
    assert [p[0] for p in pages] == [1, 2]
    assert vector_ids() == db_chunk_ids()  # FAISS and PostgreSQL agree on chunk IDs
    [stored] = uploaded_files()
    assert "handbook" not in stored and stored.endswith(".pdf")  # random storage name, not user input


def test_upload_validation_and_safety(client):
    login(client, USER_EMAIL)
    assert upload(client, "tool.exe", b"MZ\x90\x00 executable bytes", content_type="application/octet-stream").status_code == 415
    assert upload(client, "fake.pdf", b"MZ\x90\x00 executable bytes").status_code == 415
    assert upload(client, "locked.pdf", make_encrypted_pdf()).status_code == 415
    assert upload(client, "broken.pdf", b"%PDF-1.7\n not really a pdf").status_code == 415
    too_big = upload(client, "big.pdf", b"%PDF-1.7\n" + b"0" * (2 * 1024 * 1024))
    assert too_big.status_code == 413
    assert uploaded_files() == set()  # nothing rejected was kept on disk

    traversal = upload(client, "../../../etc/passwd.pdf", make_pdf("hello world"))
    assert traversal.status_code == 202 and traversal.json()["filename"] == "passwd.pdf"
    assert all(p.parent == RAG_TEST_DIR / "uploads" for p in (RAG_TEST_DIR / "uploads").iterdir())

    assert upload(client, "shared.pdf", make_pdf("x"), visibility="shared").status_code == 403  # users can't publish
    assert client.post("/api/rag/documents", headers={"X-Requested-With": ""},
                       files={"file": ("a.pdf", make_pdf("x"), "application/pdf")}).status_code == 403  # CSRF


def test_uploads_require_authentication_and_are_never_served(client):
    assert upload(client, "a.pdf", make_pdf("hello")).status_code == 401
    login(client, USER_EMAIL)
    upload(client, "a.pdf", make_pdf("hello"))
    [stored] = uploaded_files()
    for path in (f"/data/uploads/{stored}", f"/uploads/{stored}", f"/admin/../data/uploads/{stored}", f"/admin/{stored}"):
        assert client.get(path).status_code == 404


# -------------------------------------------------------------- permissions


def test_library_is_managed_by_admins_and_answers_every_user(client, fake_provider, monkeypatch):
    """The default library: only administrators add documents (enforced by the server, not just the UI), and a
    shared document answers any user's question in any chat."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_admin_only_uploads", True)
    login(client, USER_EMAIL)
    rejected = upload(client, "notes.pdf", make_pdf(REFUND_PAGE))
    assert rejected.status_code == 403 and uploaded_files() == set()

    fake_provider.rag_reply = cited_reply
    activate_provider(client)  # signs in as the admin
    response = upload(client, "refund-policy.pdf", make_pdf(REFUND_PAGE), visibility="shared")
    assert response.status_code == 202, response.text
    process_all(client)
    for email in (USER_EMAIL, USER2_EMAIL):
        login(client, email)
        body = ask(client, "How long do customers have to request a refund?").json()
        assert [c["filename"] for c in body["citations"]] == ["refund-policy.pdf"]


def test_permission_isolation(client, fake_provider):
    activate_provider(client)
    shared = upload(client, "policy.pdf", make_pdf(REFUND_PAGE), visibility="shared").json()  # admin publishes
    login(client, USER_EMAIL)
    alpha = upload(client, "alpha.pdf", make_pdf(ALPHA_SECRET)).json()
    login(client, USER2_EMAIL)
    beta = upload(client, "beta.pdf", make_pdf(SERVER_PAGE)).json()
    process_all(client)
    fake_provider.rag_reply = lambda message: "Answer [S1]."

    # user2 sees own + shared, never user1's private document
    names = {d["filename"] for d in client.get("/api/rag/documents").json()["documents"]}
    assert names == {"policy.pdf", "beta.pdf"}
    assert client.get(f"/api/rag/documents/{alpha['id']}").status_code == 404
    assert client.delete(f"/api/rag/documents/{alpha['id']}").status_code == 404
    assert replace_file(client, alpha["id"], "x.pdf", make_pdf("x")).status_code == 404

    fake_provider.calls.clear()
    response = ask(client, "What is the Project Falcon acquisition target valuation?").json()
    assert all(c["document_id"] != alpha["id"] for c in response["citations"])
    assert all(ALPHA_SECRET not in message for _, message in fake_provider.calls)  # never reached the LLM

    # Explicitly targeting another user's document remains document-scoped after ACL filtering; do not
    # substitute a general answer for the inaccessible source.
    fake_provider.calls.clear()
    targeted = ask(client, "Project Falcon valuation", document_ids=[alpha["id"]]).json()
    assert targeted["citations"] == [] and targeted["retrieved_chunks"] == 0
    assert targeted["answer"] == UNVERIFIED_FACT
    assert targeted["insufficient_evidence"] and not targeted["general_knowledge"]
    assert targeted["strategy"] == "none" and targeted["provider"] is None
    assert fake_provider.calls == []

    # An inaccessible picked PDF must not fall through to a different accessible file for an overview request.
    fake_provider.calls.clear()
    targeted_overview = ask(client, "Summarize alpha.pdf.", document_ids=[alpha["id"]]).json()
    assert targeted_overview["citations"] == [] and targeted_overview["retrieved_chunks"] == 0
    assert targeted_overview["answer"] == UNVERIFIED_FACT
    assert targeted_overview["insufficient_evidence"] and not targeted_overview["general_knowledge"]
    assert targeted_overview["strategy"] == "none" and targeted_overview["provider"] is None
    assert fake_provider.calls == []

    # Admins get no access to users' private documents either.
    login(client, ADMIN_EMAIL)
    admin_names = {d["filename"] for d in client.get("/api/rag/documents").json()["documents"]}
    assert admin_names == {"policy.pdf"}
    # Users can read shared documents but cannot change them.
    login(client, USER_EMAIL)
    assert client.delete(f"/api/rag/documents/{shared['id']}").status_code == 403
    assert document(client, shared["id"])["can_modify"] is False
    assert beta  # silence unused


# ------------------------------------------------------- answers & citations


def test_grounded_answer_with_page_citations(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(SERVER_PAGE, REFUND_PAGE)).json()
    process_all(client)
    fake_provider.rag_reply = cited_reply

    response = ask(client, "How many days do customers have to request a refund?")
    body = response.json()
    assert response.status_code == 200 and body["insufficient_evidence"] is False
    [citation] = body["citations"]
    assert citation["source_id"] == "S1" and citation["document_id"] == doc["id"]
    assert citation["filename"] == "handbook.pdf" and citation["page_number"] == 2
    assert "30 days" in citation["snippet"]

    [(system_prompt, message)] = fake_provider.calls
    assert "must come from the <source> blocks" in system_prompt and "untrusted" in system_prompt
    assert body["general_knowledge"] is False
    assert REFUND_PAGE[:40] in message
    assert "cooling units" not in message  # irrelevant chunk was not sent to the LLM


def test_open_mode_answers_from_general_knowledge_and_labels_it(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE))
    process_all(client)

    # Nothing relevant retrieved: the model answers from general knowledge, with no document text sent.
    fake_provider.rag_reply = lambda m: "Mount Tambora erupted in 1815."
    body = ask(client, "Which volcano erupted in 1815?").json()
    assert body["answer"] == "Mount Tambora erupted in 1815." and body["general_knowledge"] is True
    assert body["insufficient_evidence"] is False and body["citations"] == [] and body["strategy"] == "general"
    [(system_prompt, message)] = fake_provider.calls
    assert "general knowledge" in system_prompt and "<source" not in message and "30 days" not in message

    # A company fact the documents do not state is not replaced with general knowledge.
    fake_provider.calls.clear()
    fake_provider.rag_reply = lambda m: f"{INSUFFICIENT_MARKER}: no fee is stated."
    body = ask(client, "What fee applies to a refund request?").json()
    assert body["answer"] == UNVERIFIED_FACT and body["general_knowledge"] is False
    assert body["citations"] == [] and body["insufficient_evidence"] and len(fake_provider.calls) == 1

    # An uncited company answer is withheld. Invented source numbers do not make it general knowledge.
    fake_provider.rag_reply = lambda m: "Refunds usually take 30 days [S7]."
    body = ask(client, "How many days to request a refund?").json()
    assert body["answer"] == UNVERIFIED_FACT and body["general_knowledge"] is False and body["citations"] == []


def test_strict_mode_answers_only_from_documents(client, fake_provider):
    from app.core.config import get_settings

    strict = get_settings().model_copy(update={"rag_general_answers": False})
    app.dependency_overrides[get_settings] = lambda: strict
    try:
        activate_provider(client)
        login(client, USER_EMAIL)
        upload(client, "handbook.pdf", make_pdf(REFUND_PAGE))
        process_all(client)

        # Nothing relevant retrieved: answered without calling the LLM.
        body = ask(client, "Which volcano erupted in 1815?").json()
        assert body["insufficient_evidence"] and body["citations"] == [] and fake_provider.calls == []

        # The LLM itself reports insufficient evidence.
        fake_provider.rag_reply = lambda m: f"{INSUFFICIENT_MARKER}: the refund fee amount is not stated."
        body = ask(client, "What fee applies to a refund request?").json()
        assert body["insufficient_evidence"] and body["answer"] == "the refund fee amount is not stated."
        assert "Use ONLY the information" in fake_provider.calls[-1][0]

        # Uncited (ungrounded) answers are withheld; invented source numbers are ignored.
        fake_provider.rag_reply = lambda m: "Refunds take 30 days [S7]."
        body = ask(client, "How many days to request a refund?").json()
        assert body["retrieved_chunks"] >= 1  # sources were found, but the reply didn't cite them
        assert body["insufficient_evidence"] and body["answer"] == UNGROUNDED_ANSWER and body["citations"] == []
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_llm_failures_are_reported_safely(client, fake_provider):
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE))
    process_all(client)
    no_provider = ask(client, "refund request days")
    assert no_provider.status_code == 503 and no_provider.json()["error"]["code"] == "llm_not_configured"

    activate_provider(client)
    login(client, USER_EMAIL)
    fake_provider.chat_error = LLMTimeoutError()
    timed_out = ask(client, "refund request days")
    assert timed_out.status_code == 504 and timed_out.json()["error"]["code"] == "llm_timeout"


# ------------------------------------------------- deletion & replacement


def test_delete_removes_everything(client):
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE)).json()
    keep = upload(client, "other.pdf", make_pdf(SERVER_PAGE)).json()
    process_all(client)
    removed_ids = db_chunk_ids(doc["id"])
    assert removed_ids and removed_ids <= vector_ids()

    assert client.delete(f"/api/rag/documents/{doc['id']}").status_code == 204
    assert db_chunk_ids(doc["id"]) == set()
    assert not (removed_ids & vector_ids())  # vectors gone from FAISS
    assert vector_ids() == db_chunk_ids(keep["id"])
    assert len(uploaded_files()) == 1  # only the other document's file remains
    assert client.get(f"/api/rag/documents/{doc['id']}").status_code == 404
    actions = [r[0] for r in run_db(lambda c: c.execute(text("SELECT action FROM audit_logs"))).all()]
    assert "document.deleted" in actions


def test_replacement_swaps_content_atomically(client):
    login(client, USER_EMAIL)
    doc = upload(client, "policy.pdf", make_pdf(REFUND_PAGE)).json()
    process_all(client)
    old_ids = db_chunk_ids(doc["id"])
    [old_file] = uploaded_files()

    replaced = replace_file(client, doc["id"], "policy-v2.pdf", make_pdf(SERVER_PAGE))
    assert replaced.status_code == 202
    body = replaced.json()
    assert body["status"] == "pending" and body["version"] == 2 and body["searchable"]  # v1 still searchable
    assert db_chunk_ids(doc["id"]) == old_ids and old_file in uploaded_files()
    history = client.get(f"/api/rag/documents/{doc['id']}/versions")
    assert history.status_code == 200
    assert history.json()[0]["version"] == 1 and history.json()[0]["filename"] == "policy.pdf"
    download = client.get(f"/api/rag/documents/{doc['id']}/versions/1/file")
    assert download.status_code == 200 and len(download.content) == history.json()[0]["size_bytes"]

    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{doc['id']}/versions").status_code == 404
    client.cookies.clear()
    login(client, USER_EMAIL)

    process_all(client)
    body = document(client, doc["id"])
    assert body["status"] == "ready" and body["indexed_version"] == 2 and body["filename"] == "policy-v2.pdf"
    new_ids = db_chunk_ids(doc["id"])
    assert new_ids.isdisjoint(old_ids)  # chunk IDs are never reused
    assert vector_ids() == new_ids


def test_failed_replacement_keeps_previous_version_searchable(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = upload(client, "policy.pdf", make_pdf(REFUND_PAGE)).json()
    process_all(client)
    old_ids = db_chunk_ids(doc["id"])

    replace_file(client, doc["id"], "scan.pdf", make_pdf(""))  # valid PDF but no extractable text
    process_all(client)
    body = document(client, doc["id"])
    assert body["status"] == "failed" and body["error_code"] == "no_text"
    assert "previously indexed version is still searchable" in body["error_message"]
    assert body["searchable"] and db_chunk_ids(doc["id"]) == old_ids == vector_ids()

    fake_provider.rag_reply = cited_reply
    citation = ask(client, "refund request days").json()["citations"][0]
    assert citation["filename"] == "policy.pdf"  # cites the version that is actually indexed


def test_stale_job_is_discarded_after_concurrent_replacement(client):
    login(client, USER_EMAIL)
    doc = upload(client, "a.pdf", make_pdf(REFUND_PAGE)).json()
    worker = app.state.rag.worker
    job = client.portal.call(worker._claim)  # worker picked up version 1...
    replace_file(client, doc["id"], "b.pdf", make_pdf(SERVER_PAGE))  # ...then the user replaced it
    client.portal.call(partial(worker._fail, job, "processing_error", "boom", permanent=True))
    assert document(client, doc["id"])["status"] == "pending"  # stale result ignored
    process_all(client)
    assert document(client, doc["id"])["indexed_version"] == 2


# --------------------------------------------------------- failures & retry


def test_transient_failures_retry_then_fail_and_can_be_reprocessed(client, fake_embedder):
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE)).json()
    fake_embedder.fail = True

    assert process_one(client)
    body = document(client, doc["id"])
    assert body["status"] == "pending" and body["attempts"] == 1 and body["error_code"] == "processing_error"
    process_all(client)  # retries until the attempt limit
    body = document(client, doc["id"])
    assert body["status"] == "failed" and body["attempts"] == 3 and not body["searchable"]
    assert db_chunk_ids() == set() and vector_ids() == set()

    fake_embedder.fail = False
    assert client.post(f"/api/rag/documents/{doc['id']}/reprocess").status_code == 202
    process_all(client)
    assert document(client, doc["id"])["status"] == "ready"


# ------------------------------------------------- persistence & rebuilding


def test_index_survives_restart_and_rebuilds_when_lost(client):
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE, SERVER_PAGE))
    process_all(client)
    expected = db_chunk_ids()
    client.__exit__(None, None, None)  # stop the app (lifespan shutdown)

    with TestClient(app, headers=CSRF) as restarted:  # fresh process state, index loaded from disk
        assert vector_ids() == expected and not app.state.rag.vector_store.needs_rebuild

    (RAG_TEST_DIR / "vector_index" / "index.faiss").write_bytes(b"corrupted")
    with TestClient(app, headers=CSRF) as restarted:
        assert app.state.rag.vector_store.needs_rebuild and vector_ids() == set()
        restarted.portal.call(app.state.rag.worker.startup)  # what the background worker does on boot
        assert vector_ids() == expected and not app.state.rag.vector_store.needs_rebuild


def test_index_rebuild_endpoint_is_admin_only(client):
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE))
    process_all(client)
    assert client.post("/api/rag/index/rebuild").status_code == 403
    login(client, ADMIN_EMAIL)
    body = client.post("/api/rag/index/rebuild").json()
    assert body["mode"] == "rebuild" and body["vectors"] == len(db_chunk_ids())
    assert client.get("/api/rag/index").json()["vectors"] == len(db_chunk_ids())


def test_vector_store_status_is_admin_only_and_matches_the_database(client):
    assert client.get("/api/rag/vector-store").status_code == 401
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE, SERVER_PAGE))
    process_all(client)
    assert client.get("/api/rag/vector-store").status_code == 403

    login(client, ADMIN_EMAIL)
    body = client.get("/api/rag/vector-store").json()
    assert body["backend"] == "faiss" and body["supported_backends"] == ["faiss", "qdrant"]
    assert body["dimension"] == body["embedding_dimension"] == 384
    assert body["status"] == "healthy" and body["issues"] == []
    documents, knowledge = body["indexes"]
    assert documents["name"] == "documents" and documents["status"] == "healthy"
    assert documents["vectors"] == documents["expected_vectors"] == len(db_chunk_ids())
    assert knowledge["name"] == "knowledge" and knowledge["vectors"] == knowledge["expected_vectors"] == 0
    assert not any(key in str(body) for key in ("vector_index", "Refund policy"))  # no paths or document text

    # The index falling behind PostgreSQL, or needing a rebuild, is reported (and nothing is changed).
    store = app.state.rag.vector_store
    store.apply(remove_ids=[min(db_chunk_ids())])
    behind = client.get("/api/rag/vector-store").json()
    assert behind["status"] == "degraded" and behind["indexes"][0]["status"] == "out_of_sync"
    store.needs_rebuild = True
    broken = client.get("/api/rag/vector-store").json()
    assert broken["indexes"][0]["status"] == "needs_rebuild" and broken["issues"]


def test_embedding_model_change_requeues_documents(client):
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE)).json()
    process_all(client)
    run_db(lambda c: c.execute(text("UPDATE documents SET embedding_model = 'old/model'")))
    assert document(client, doc["id"])["searchable"] is False  # old vectors are not mixed with new queries
    assert client.portal.call(app.state.rag.worker.requeue_stale_embeddings) == 1
    process_all(client)
    assert document(client, doc["id"])["searchable"] is True


# ------------------------------------------------------------------- privacy


def test_document_content_and_questions_are_not_logged(client, fake_provider, caplog):
    caplog.set_level(logging.DEBUG)
    activate_provider(client)
    login(client, USER_EMAIL)
    fake_provider.rag_reply = cited_reply
    doc = upload(client, "handbook.pdf", make_pdf(ALPHA_SECRET, REFUND_PAGE)).json()
    process_all(client)
    question = "What is the Project Falcon acquisition target valuation?"
    ask(client, question)
    replace_file(client, doc["id"], "handbook2.pdf", make_pdf(ALPHA_SECRET))
    process_all(client)
    client.delete(f"/api/rag/documents/{doc['id']}")
    for secret in (ALPHA_SECRET, "Project Falcon", question, "30 days of purchase"):
        assert secret not in caplog.text


def test_embedding_model_errors_are_retried_not_treated_as_missing_files(client, monkeypatch):
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE)).json()

    def model_files_missing(texts):
        raise FileNotFoundError("model weights not found")

    monkeypatch.setattr(app.state.rag.embedder, "embed", model_files_missing)
    process_one(client)
    body = document(client, doc["id"])
    assert body["status"] == "pending" and body["error_code"] == "processing_error"  # transient, will retry

    monkeypatch.undo()
    process_all(client)
    assert document(client, doc["id"])["status"] == "ready"


def test_missing_stored_file_fails_permanently(client):
    login(client, USER_EMAIL)
    doc = upload(client, "handbook.pdf", make_pdf(REFUND_PAGE)).json()
    for path in (RAG_TEST_DIR / "uploads").iterdir():
        path.unlink()
    process_one(client)
    body = document(client, doc["id"])
    assert body["status"] == "failed" and body["error_code"] == "file_missing" and body["attempts"] == 1


def test_greetings_get_a_friendly_reply_without_search_or_llm(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "handbook.pdf", make_pdf(REFUND_PAGE))
    process_all(client)
    for text_ in ("hi", "Hello!", "salam", "Assalam o Alaikum", "thanks"):
        body = ask(client, text_).json()
        assert body["insufficient_evidence"] is False and body["citations"] == [] and body["retrieved_chunks"] == 0
    assert fake_provider.calls == []
    # A real question that merely starts with a greeting still goes through retrieval.
    fake_provider.rag_reply = cited_reply
    assert ask(client, "hi, how many days do I have to request a refund?").json()["citations"]


# ------------------------------------------------- overview & conversation

def _all_pages_reply(message: str) -> str:
    count = message.count("<source ")
    return "Overview: " + " ".join(f"point [S{i}]." for i in range(1, count + 1))


def test_overview_question_uses_the_whole_document_in_page_order(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    pages = [f"Section {n}: topic number {n} about subject{n} details." for n in range(1, 7)]
    doc = upload(client, "report.pdf", make_pdf(*pages)).json()
    process_all(client)
    fake_provider.rag_reply = _all_pages_reply

    body = ask(client, "check this report").json()
    assert body["strategy"] == "whole_document" and body["retrieved_chunks"] == 6
    [(_, message)] = fake_provider.calls
    positions = [message.index(f"Section {n}:") for n in range(1, 7)]
    assert positions == sorted(positions)  # every page, in order (not just the top-k)
    assert [c["page_number"] for c in body["citations"]] == [1, 2, 3, 4, 5, 6]
    assert [c["source_id"] for c in body["citations"]] == [f"S{i}" for i in range(1, 7)]
    assert all(c["document_id"] == doc["id"] for c in body["citations"])


def test_follow_up_refers_to_the_document_from_the_previous_answer(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    first = upload(client, "refunds.pdf", make_pdf(REFUND_PAGE)).json()
    upload(client, "servers.pdf", make_pdf(SERVER_PAGE))
    process_all(client)
    fake_provider.rag_reply = cited_reply

    # "summary of this" with two documents: the focus from the previous answer decides which one.
    body = client.post("/api/rag/chat", json={
        "question": "summary of this",
        "history": [{"question": "refund window?", "answer": "30 days [S1]."}],
        "focus_document_ids": [first["id"]],
    }).json()
    assert body["strategy"] == "whole_document"
    assert {c["document_id"] for c in body["citations"]} == {first["id"]}
    _, message = fake_provider.calls[-1]
    assert "CONVERSATION" in message and "cooling units" not in message

    # A vague follow-up that matches nothing on its own is answered from the focused document.
    body = client.post("/api/rag/chat", json={
        "question": "explain that in more detail",
        "history": [{"question": "refund window?", "answer": "30 days [S1]."}],
        "focus_document_ids": [first["id"]],
    }).json()
    assert body["strategy"] == "whole_document"
    assert {c["document_id"] for c in body["citations"]} == {first["id"]}


def test_focus_ids_cannot_reach_other_users_documents(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    secret = upload(client, "alpha.pdf", make_pdf(ALPHA_SECRET)).json()
    process_all(client)
    login(client, USER2_EMAIL)
    fake_provider.rag_reply = cited_reply
    for question in ("summary of this", "explain that in more detail"):
        body = client.post("/api/rag/chat", json={
            "question": question,
            "history": [{"question": "x", "answer": "y"}],
            "focus_document_ids": [secret["id"]],
            "document_ids": [secret["id"]],
        }).json()
        assert body["citations"] == [] and body["retrieved_chunks"] == 0
    assert all(ALPHA_SECRET not in message for _, message in fake_provider.calls)


# ---------------------------------------------------------------- page images

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_credentials_stay_out_of_answers_snippets_and_page_images(client, fake_provider):
    """Even a model that repeats a secret cannot leak it: the answer and citation snippets are scrubbed, and a page
    that shows a credential (or whose text is unknown) is not returned as an image."""
    secret = "sunshine"
    fake_provider.rag_reply = lambda message: f"The server room password is {secret} [S1]."
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = upload(
        client, "it-runbook.pdf",
        make_pdf(REFUND_PAGE, f"Server room access\nThe server room password is {secret}.", ""),
    ).json()
    process_all(client)

    body = ask(client, "What is the server room password?").json()
    assert body["citations"] and secret not in json.dumps(body)
    assert all(secret not in message for _, message in fake_provider.calls)  # never sent to the model

    images = client.get(f"/api/rag/documents/{doc['id']}/pages/1/image")
    assert images.status_code == 200 and images.content.startswith(PNG_MAGIC)
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/2/image").status_code == 403  # credential
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/3/image").status_code == 403  # no text: unknown

    fake_provider.calls.clear()
    pages = ask(client, "create img of this report").json()
    assert [i["page_number"] for i in pages["page_images"]] == [1]


def test_photo_request_shows_real_pages_without_calling_the_llm(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "report.pdf", make_pdf(REFUND_PAGE, SERVER_PAGE, ALPHA_SECRET))
    process_all(client)

    body = ask(client, "create img of this report").json()
    assert body["strategy"] == "page_images" and body["insufficient_evidence"] is False
    assert [i["page_number"] for i in body["page_images"]] == [1, 2, 3]
    assert fake_provider.calls == []  # rendered locally; nothing sent to the LLM
    image = client.get(body["page_images"][0]["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content.startswith(PNG_MAGIC)

    single = ask(client, "page 2 ki photo banao").json()
    assert [i["page_number"] for i in single["page_images"]] == [2]
    missing = ask(client, "page 9 ki photo").json()
    assert missing["page_images"] == [] and "no page 9" in missing["answer"]


def test_page_images_follow_document_permissions(client):
    login(client, USER_EMAIL)
    doc = upload(client, "private.pdf", make_pdf(ALPHA_SECRET)).json()
    process_all(client)
    url = f"/api/rag/documents/{doc['id']}/pages/1/image"
    assert client.get(url).status_code == 200
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/5/image").status_code == 404
    assert client.get(url + "?width=99999").status_code == 422  # bounded render size

    login(client, USER2_EMAIL)
    assert client.get(url).status_code == 404  # another user's private document
    assert ask(client, "photo of this report").json()["page_images"] == []
    client.cookies.clear()
    assert client.get(url).status_code == 401


# --------------------------------------------------------------------- charts

ROLLOUT_PAGE = "Remaining gaps: domain joining pending 18 entries; drive mapping pending 12 entries; monitoring 18 to confirm."


def test_chart_request_returns_verified_chart_data(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "it-report.pdf", make_pdf(REFUND_PAGE, ROLLOUT_PAGE))
    process_all(client)

    def chart_reply(message: str) -> str:
        page = 2 if 'page="2"' in message else 1
        tag = next(f"S{i}" for i in range(1, 10) if f'id="S{i}" document="it-report.pdf" page="{page}"' in message)
        return json.dumps({
            "title": "Next activities", "unit": "entries", "layout": "grouped",
            "summary": f"Pending rollout work by area [{tag}].",
            "points": [
                {"category": "Domain joining", "series": "Pending", "value": 18, "source": tag},
                {"category": "Drive mapping", "series": "Pending", "value": 12, "source": tag},
                {"category": "Invented", "series": "Pending", "value": 77, "source": tag},
            ],
        })

    fake_provider.rag_reply = chart_reply
    body = ask(client, "create graph here of next activity").json()
    assert body["strategy"] == "chart" and body["insufficient_evidence"] is False
    chart = body["chart"]
    assert chart["categories"] == ["Domain joining", "Drive mapping"]  # the invented 77 was dropped
    assert chart["series"] == [{"name": "Pending", "values": [18.0, 12.0], "sources": ["S1", "S1"]}]
    assert body["citations"][0]["page_number"] == 2
    [(system_prompt, _)] = fake_provider.calls
    assert "never invent numbers" in system_prompt


def test_chart_requests_respect_permissions(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    secret = upload(client, "private.pdf", make_pdf(ROLLOUT_PAGE)).json()
    process_all(client)
    login(client, USER2_EMAIL)
    body = client.post("/api/rag/chat", json={"question": "graph of this report", "focus_document_ids": [secret["id"]]}).json()
    assert body["chart"] is None and body["insufficient_evidence"]
    assert fake_provider.calls == []


def test_this_report_follow_up_stays_on_the_document_being_discussed(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    sip = upload(client, "sip-report.pdf", make_pdf("SIP telephony versus mobile SIM report: purpose is a commercial evaluation of calling options.")).json()
    upload(client, "it-report.pdf", make_pdf("IT infrastructure report: purpose is to report rollout progress and the main agenda for the CEO."))
    process_all(client)
    fake_provider.rag_reply = lambda m: "The purpose is a commercial evaluation of calling options [S1]."
    history = [{"question": "what is sip report?", "answer": "It compares SIP and SIM [S1]."}]

    for question in ("what is main agend of this report?", "what is the purpose of it?", "who prepared this document?"):
        fake_provider.calls.clear()
        body = client.post("/api/rag/chat", json={"question": question, "history": history, "focus_document_ids": [sip["id"]]}).json()
        assert {c["document_id"] for c in body["citations"]} == {sip["id"]}, question
        assert all("IT infrastructure report" not in message for _, message in fake_provider.calls), question
