"""Query understanding and hybrid retrieval (docs/hybrid-retrieval.md): typo tolerance from the corpus vocabulary,
acronyms, follow-ups that borrow context only when they need it, Roman Urdu, broad questions, a repetitive
document that must not fill the results, ACLs at every stage, and the semantic fallback. All text is synthetic."""

import pytest

from app.main import app
from app.rag import query_analysis
from app.rag.query_analysis import edit_distance, mask_acronyms, query_words
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, process_all, upload
from tests.test_access_control import set_access

POLICY = (
    "IT acceptable use policy. This IT policy applies to every employee. Passwords must be changed every 90 days. "
    "Personal cloud storage must not be used for company files. Door access badges are personal."
)
FINDINGS = (
    "Site inspection findings. Several issues require attention: 9 door sensors are offline, 6 of them at the "
    "Pier 4 warehouse. Backup retention varies between 5 and 14 days."
)
MEMO = "Payment memo from the IT department. The fibre internet link payment was approved by the CFO."
REGISTER = "Quarterly risk register. Key challenges: delayed sensor deliveries and staff turnover."
SECRET = "Project Zephyrine budget note. The Zephyrine launch budget is PKR 9,900,000."


def access_log(rows: int = 600) -> bytes:
    lines = ["Log date,User,Door,Event,Remark"]
    for n in range(rows):  # every row repeats "policy" and "issue": a large repetitive document
        lines.append(f"2026-09-{1 + n % 28:02d},staff-{n % 40:03d},Door {n % 4},Access granted,Badge read per policy; no issue")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def library(client):
    activate_provider(client)  # signs in as the admin
    ids = {}
    for name, body in (("it-policy.txt", POLICY), ("findings.txt", FINDINGS), ("payment-memo.txt", MEMO),
                       ("risk-register.txt", REGISTER)):
        ids[name] = upload(client, name, body.encode(), visibility="shared", content_type="text/plain").json()["id"]
    ids["access-log.csv"] = upload(client, "access-log.csv", access_log(), visibility="shared", content_type="text/csv").json()["id"]
    secret = upload(client, "zephyrine.txt", SECRET.encode(), content_type="text/plain").json()["id"]
    assert set_access(client, secret, "restricted", users=[USER2_EMAIL]).status_code == 200
    ids["zephyrine.txt"] = secret
    process_all(client)
    return ids


def search(email: str, question: str, *, improved: bool = True, history: list[tuple[str, str]] = (), **settings_update):
    """Production run_chat (no provider: the answer step is a stub) for one user; returns (strategy, passages)."""
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.db.models import User
    from app.rag.answer import Turn
    from app.rag.chat import run_chat
    from app.schemas.chat import ChatResponse

    class Offline:
        async def chat(self, message, *, system_prompt=None):
            return ChatResponse(reply="NONE" if "TABLES in" in message else "Stub [S1].", model="m", provider="p")

    async def get_llm():
        return Offline()

    settings = get_settings().model_copy(update={
        "rag_hybrid_retrieval": improved, "rag_query_analysis": improved, "rag_general_answers": False, **settings_update,
    })

    async def go():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            result = await run_chat(db, user, app.state.rag, settings, get_llm, question,
                                    history=[Turn(q, a) for q, a in history], document_ids=None, focus_document_ids=None)
            return result.strategy, result.passages

    from tests.conftest import client as _  # noqa: F401  (the app loop is running under the client fixture)

    return _PORTAL[0].call(go)


_PORTAL: list = []


@pytest.fixture(autouse=True)
def _portal(client):
    _PORTAL[:] = [client.portal]
    yield
    _PORTAL.clear()


def files(passages) -> list[str]:
    return [chunk.filename for chunk in passages]


# ------------------------------------------------------------------ query analysis (no database)


def test_edit_distance_counts_a_transposition_once():
    assert edit_distance("polciy", "policy", 2) == 1
    assert edit_distance("challange", "challenge", 2) == 1
    assert edit_distance("pasword", "password", 2) == 1
    assert edit_distance("kitne", "kites", 1) == 2  # beyond the limit for a five-letter word


def test_words_drop_function_words_but_keep_acronyms():
    assert query_words("What is the IT policy?") == ["it", "policy"]
    assert query_words("what is it about?") == []  # the pronoun is a function word
    assert query_words("password kitne din baad change karna hota hai?") == ["password", "kitne", "din", "baad", "change"]
    assert mask_acronyms("what is IT polciy") == "what is __ polciy"
    assert mask_acronyms("WHAT IS IT") == "WHAT IS IT"  # all capitals: no acronym can be told apart


# ------------------------------------------------------------------ retrieval


def test_a_typo_or_an_acronym_still_finds_the_document(client, library):
    login(client, USER_EMAIL)
    for question in ("what is IT polciy", "How often must pasword be changed?", "What is IT policy?"):
        _, passages = search(USER_EMAIL, question)
        assert passages and passages[0].filename == "it-policy.txt", question
    _, baseline = search(USER_EMAIL, "what is IT polciy", improved=False)
    assert "it-policy.txt" not in files(baseline)[:1]  # semantic search alone misses it (the defect this fixes)


def test_a_follow_up_borrows_context_only_when_it_has_no_topic_of_its_own(client, library):
    strategy, passages = search(USER_EMAIL, "what is IT polciy", history=[("What is the major issue in my company?", "(none)")])
    assert strategy == "semantic" and passages[0].filename == "it-policy.txt"  # independent: no context borrowed
    strategy, passages = search(USER_EMAIL, "and what about passwords?", history=[("What does the IT policy say about cloud storage?", "...")])
    assert strategy == "semantic_with_context" and passages[0].filename == "it-policy.txt"
    strategy, passages = search(USER_EMAIL, "where are most of them?", history=[("How many door sensors are offline?", "9.")])
    assert strategy == "semantic_with_context" and "findings.txt" in files(passages)


def test_it_in_a_follow_up_is_the_previous_questions_document(client, library):
    history = [("What does the IT policy say about cloud storage?", "(refused, nothing cited)")]
    strategy, passages = search(USER_EMAIL, "what are the challanges in it?", history=history)
    assert strategy == "semantic_with_context"
    assert set(files(passages)) == {"it-policy.txt"}  # not the risk register's "challenges"
    _, baseline = search(USER_EMAIL, "what are the challanges in it?", history=history, improved=False)
    assert baseline is not None


def test_broad_and_roman_urdu_questions(client, library):
    _, passages = search(USER_EMAIL, "What problems need attention?")
    assert "findings.txt" in files(passages)  # matched on its distinctive words, though its similarity is low
    _, passages = search(USER_EMAIL, "password kitne din baad change karna hota hai?")
    assert files(passages)[0] == "it-policy.txt"


def test_a_repetitive_document_does_not_fill_the_results(client, library):
    _, before = search(USER_EMAIL, "What is the policy on door access badges?", improved=False)
    _, after = search(USER_EMAIL, "What is the policy on door access badges?")
    assert files(after)[0] == "it-policy.txt"
    assert files(after).count("access-log.csv") <= 3 and files(after).count("access-log.csv") <= files(before).count("access-log.csv")
    _, issue = search(USER_EMAIL, "Is there any issue with backup retention?")
    assert files(issue)[0] == "findings.txt"


def test_acl_applies_to_lexical_matches_and_corrections(client, library):
    from app.rag.retrieval import _scope

    for question in ("Zephyrine launch budget", "Zephyrin launch budjet"):
        _, outsider = search(USER_EMAIL, question)
        assert "zephyrine.txt" not in files(outsider), question
        _, reader = search(USER2_EMAIL, question)
        assert files(reader)[0] == "zephyrine.txt", question

    async def lexical_for(email: str):  # the lexical SQL itself carries the ACL predicate
        from sqlalchemy import select

        from app.db.models import User

        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            analysis = await query_analysis.analyze_query(db, ["Zephyrine budget"], correct=True)
            hits = await query_analysis.lexical_candidates(db, _scope(user, app.state.rag.embedder.model_name), analysis, 50)
            return {chunk_id for chunk_id, *_ in hits}

    secret_chunks = set(run_db(lambda c: c.exec_driver_sql(
        "SELECT id FROM document_chunks WHERE document_id = %s" % repr(library["zephyrine.txt"]))).scalars())
    assert not (_PORTAL[0].call(lexical_for, USER_EMAIL) & secret_chunks)
    assert _PORTAL[0].call(lexical_for, USER2_EMAIL) & secret_chunks


def test_lexical_failure_falls_back_to_semantic_search(client, library, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("lexical search down")

    monkeypatch.setattr(query_analysis, "analyze_query", broken)
    monkeypatch.setattr("app.rag.retrieval.analyze_query", broken)
    monkeypatch.setattr("app.rag.chat.analyze_query", broken)
    _, passages = search(USER_EMAIL, "How often must passwords be changed?")
    assert passages and passages[0].filename == "it-policy.txt"


def test_the_vocabulary_follows_the_chunks(client, library):
    def chunks_with(term: str) -> int:
        return run_db(lambda c: c.exec_driver_sql("SELECT chunk_count FROM rag_terms WHERE term = %s" % repr(term))).scalar() or 0

    assert chunks_with("zephyrine") == 1
    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/rag/documents/{library['zephyrine.txt']}").status_code == 204
    assert chunks_with("zephyrine") == 0  # removed with its chunks
