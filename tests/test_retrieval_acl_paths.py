"""Candidate-first retrieval (used for large readable scopes) must return exactly what pre-filtered retrieval
returns, for every kind of permission, and must never surface a chunk the user cannot read: not when the
best matches are unreadable, not right after access is revoked, and not when the index is stale.
Runs against real PostgreSQL. All documents and people are synthetic."""

import uuid

import pytest
from sqlalchemy import select, text

from app.db.models import User
from app.main import app
from app.rag import retrieval
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import process_all, upload
from tests.test_access_control import new_group, set_access, user_id

# Every text shares words with the questions, so unreadable documents compete for the top ranks.
TEXTS = {
    "shared-boiler.txt": "Boiler maintenance schedule: the boiler valve inspection happens every spring.",
    "group-payroll.txt": "Payroll calendar: payroll runs on the boiler room terminal every second Friday.",
    "user2-only.txt": "Contract for valve supplier: valve inspection fees and payroll invoice terms.",
    "admin-private.txt": "Board notes: boiler replacement budget and valve inspection budget approved.",
}
QUESTIONS = ("When is the valve inspection?", "When does payroll run?", "What is the boiler budget?")


def _upload(client, name: str, body: str) -> str:
    response = upload(client, name, body.encode(), content_type="text/plain")  # plain text: no PDF pipeline
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _seed(client) -> dict[str, str]:
    """As admin: one shared, one group-restricted (USER), one user-restricted (USER2) and one private document;
    each regular user also owns a private one."""
    login(client, ADMIN_EMAIL)
    ids = {name: _upload(client, name, body) for name, body in TEXTS.items()}
    group = new_group(client, "Finance")
    assert client.post(f"/api/admin/access/groups/{group}/members", json={"email": USER_EMAIL}).status_code == 204
    assert set_access(client, ids["shared-boiler.txt"], "shared").status_code == 200
    assert set_access(client, ids["group-payroll.txt"], "restricted", groups=[group]).status_code == 200
    assert set_access(client, ids["user2-only.txt"], "restricted", users=[USER2_EMAIL]).status_code == 200
    for email in (USER_EMAIL, USER2_EMAIL):
        login(client, email)
        ids[email] = _upload(client, f"{email}-notes.txt", f"My valve inspection payroll notes, {email}.")
    process_all(client)
    ids["group"] = group
    return ids


def _retrieve(client, email: str, question: str, *, prefilter: bool, min_similarity: float = -1.0, document_ids=None):
    """(chunk id, score) pairs from the production `retrieve`, forced onto one strategy."""
    previous = retrieval.PREFILTER_MAX_CHUNKS
    retrieval.PREFILTER_MAX_CHUNKS = 10**12 if prefilter else -1

    async def run():
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            chunks = await retrieval.retrieve(
                db, user, app.state.rag.embedder, app.state.rag.vector_store, question,
                top_k=3, min_similarity=min_similarity, max_context_chars=100_000, document_ids=document_ids,
            )
            return [(c.chunk_id, round(c.score, 5), str(c.document_id)) for c in chunks]

    try:
        return client.portal.call(run)
    finally:
        retrieval.PREFILTER_MAX_CHUNKS = previous


def _same_ranking(a: list, b: list) -> bool:
    """Same scores, and the same chunks for every score above the last one (chunks tied at the cut-off may
    be ordered differently by the filtered and unfiltered searches, which is not a difference in result)."""
    if [s for _, s, _ in a] != [s for _, s, _ in b]:
        return False
    cutoff = a[-1][1] if a else None
    return [i for i, s, _ in a if s != cutoff] == [i for i, s, _ in b if s != cutoff]


def _readable(client, email: str) -> set[str]:
    login(client, email)
    return {d["id"] for d in client.get("/api/rag/documents").json()["documents"]}


def test_candidate_first_matches_prefiltered_retrieval_for_every_permission_kind(client):
    ids = _seed(client)
    for email in (ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL):
        readable = _readable(client, email)
        for question in QUESTIONS:
            for min_similarity in (-1.0, 0.15):
                exact = _retrieve(client, email, question, prefilter=True, min_similarity=min_similarity)
                candidates = _retrieve(client, email, question, prefilter=False, min_similarity=min_similarity)
                assert _same_ranking(candidates, exact), (email, question, min_similarity, candidates, exact)
                assert {doc for _, _, doc in candidates} <= readable
    # The group member reads the group document, the outsider does not; USER2 reads its user-level grant.
    assert ids["group-payroll.txt"] in {d for _, _, d in _retrieve(client, USER_EMAIL, QUESTIONS[1], prefilter=False)}
    assert ids["group-payroll.txt"] not in {d for _, _, d in _retrieve(client, USER2_EMAIL, QUESTIONS[1], prefilter=False)}
    assert ids["user2-only.txt"] in {d for _, _, d in _retrieve(client, USER2_EMAIL, QUESTIONS[0], prefilter=False)}
    # A document filter narrows both strategies the same way.
    focus = [uuid.UUID(ids["shared-boiler.txt"]), uuid.UUID(ids["admin-private.txt"])]
    focused = _retrieve(client, USER_EMAIL, QUESTIONS[0], prefilter=False, document_ids=focus)
    assert _same_ranking(focused, _retrieve(client, USER_EMAIL, QUESTIONS[0], prefilter=True, document_ids=focus))
    assert {d for _, _, d in focused} == {ids["shared-boiler.txt"]}


def test_candidate_first_falls_back_to_prefiltering_when_readable_candidates_are_rare(client, monkeypatch):
    _seed(client)
    fallbacks = []
    original = retrieval._readable_chunk_ids

    async def counted(db, scope):
        fallbacks.append(1)
        return await original(db, scope)

    monkeypatch.setattr(retrieval, "_readable_chunk_ids", counted)
    monkeypatch.setattr(retrieval, "CANDIDATE_POOLS", (1,))  # one candidate: never enough for top_k = 3
    exact = _retrieve(client, USER2_EMAIL, QUESTIONS[2], prefilter=True)
    fallbacks.clear()
    assert _same_ranking(_retrieve(client, USER2_EMAIL, QUESTIONS[2], prefilter=False), exact)
    assert fallbacks  # the pool was too small, so the exact pre-filtered search answered

    monkeypatch.setattr(retrieval, "CANDIDATE_POOLS", (2, 64))  # the second pool covers the whole index
    fallbacks.clear()
    assert _same_ranking(_retrieve(client, USER2_EMAIL, QUESTIONS[2], prefilter=False), exact)
    assert not fallbacks


def test_candidate_first_applies_revocation_on_the_next_search(client):
    ids = _seed(client)
    payroll = ids["group-payroll.txt"]
    assert payroll in {d for _, _, d in _retrieve(client, USER_EMAIL, QUESTIONS[1], prefilter=False)}

    login(client, ADMIN_EMAIL)
    member = user_id(USER_EMAIL)
    assert client.delete(f"/api/admin/access/groups/{ids['group']}/members/{member}").status_code == 204
    assert payroll not in {d for _, _, d in _retrieve(client, USER_EMAIL, QUESTIONS[1], prefilter=False)}

    # Narrowing a shared document to restricted (no grant for USER) also applies immediately.
    shared = ids["shared-boiler.txt"]
    assert shared in {d for _, _, d in _retrieve(client, USER_EMAIL, QUESTIONS[0], prefilter=False)}
    assert set_access(client, shared, "restricted", users=[USER2_EMAIL]).status_code == 200
    assert shared not in {d for _, _, d in _retrieve(client, USER_EMAIL, QUESTIONS[0], prefilter=False)}


@pytest.mark.parametrize("prefilter", [False, True])
def test_stale_index_entries_are_never_returned(client, prefilter):
    """Chunks deleted in PostgreSQL but still in the vector index (e.g. the index update failed) stay hidden."""
    ids = _seed(client)
    shared = ids["shared-boiler.txt"]
    before = app.state.rag.vector_store.count()
    run_db(lambda c: c.execute(text("DELETE FROM documents WHERE id = :id"), {"id": shared}))
    assert app.state.rag.vector_store.count() == before  # the index was not told
    for email in (ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL):
        for question in QUESTIONS:
            assert shared not in {d for _, _, d in _retrieve(client, email, question, prefilter=prefilter)}
