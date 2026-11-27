"""Conversation context and safe fallback (docs/conversation-context.md): per-turn source state, follow-ups resolved
through it, named documents before the conversation's focus, independent questions borrowing nothing, ACLs on
tracked documents, and general answers gated off document questions. All text is synthetic."""

import uuid

from app.rag.answer import NO_ANSWER_NOTE, Turn
from tests.conftest import USER2_EMAIL, USER_EMAIL, login
from tests.test_document_discovery import _portal, chat, contents, files, library  # noqa: F401  (fixtures)

DASHBOARD = "billing-dashboard-capture.txt"
JULY = "Internet-Bills-July2026.docx"
AUGUST = "Internet-Bills-August2026.docx"


def turn(question: str, answer: str | None, *documents: str, grounded: bool = False) -> Turn:
    return Turn(question, answer if answer is not None else NO_ANSWER_NOTE, grounded=grounded,
                documents=tuple(uuid.UUID(d) for d in documents))


def ids(*documents: str) -> list[uuid.UUID]:
    return [uuid.UUID(d) for d in documents]


def test_a_refused_turn_that_named_a_document_becomes_the_focus(client, library):
    """The 19 October incident: after a screenshot question, "net ka bill yr" follows "july ka kitna bill tha"
    (refused, but it named the July bill), so it is about the July bill, not the screenshot."""
    history = [
        turn("What is in the billing dashboard capture?", "Outstanding balance $0.00. [S1]", library[DASHBOARD], grounded=True),
        turn("july ka kitna bill tha", None, library[JULY]),
    ]
    result = chat(USER_EMAIL, "net ka bill yr", history=history, focus=ids(library[JULY]))
    assert files(result) and set(files(result)) == {JULY}
    assert "$0.00" not in contents(result)


def test_a_different_month_named_after_a_refusal_overrides_the_previous_document(client, library):
    history = [turn("july ka kitna bill tha", None, library[JULY])]
    result = chat(USER_EMAIL, "aur August2026 ka?", history=history, focus=ids(library[JULY]))
    assert set(files(result)) == {AUGUST}
    assert "47,800" in contents(result)


def test_a_named_follow_up_does_not_borrow_another_documents_question(client, library):
    """"july ka kitna bill tha" after a screenshot question names the July bill: it is searched alone, inside the July
    bill, which arrives whole (its total included), with nothing from the screenshot."""
    history = [turn("What is in the billing dashboard capture?", "Outstanding balance $0.00. [S1]", library[DASHBOARD], grounded=True)]
    result = chat(USER_EMAIL, "july ka kitna bill tha", history=history, focus=ids(library[DASHBOARD]))
    assert set(files(result)) == {JULY} and "41,250" in contents(result)


def test_this_file_settles_a_name_several_documents_share(client, library):
    """Both memos match "internet bill"; "this file" means the one the conversation is about."""
    history = [turn("What was the August 2026 internet bill total?", "Rs. 47,800. [S1]", library[AUGUST], grounded=True)]
    result = chat(USER_EMAIL, "What is the internet bill total in this file?", history=history, focus=ids(library[AUGUST]))
    assert set(files(result)) == {AUGUST}


def test_named_documents_win_over_the_conversations_focus_in_a_follow_up(client, library):
    history = [turn("What was the July 2026 internet bill total?", "Rs. 41,250. [S1]", library[JULY], grounded=True)]
    result = chat(USER_EMAIL, "aur August2026 ka?", history=history, focus=ids(library[JULY]))
    assert files(result) and set(files(result)) == {AUGUST}


def test_an_independent_question_borrows_nothing_from_the_conversation(client, library):
    history = [turn("What was the July 2026 internet bill total?", "Rs. 41,250. [S1]", library[JULY], grounded=True)]
    result = chat(USER_EMAIL, "What did the Lahore DC cooling audit find?", history=history, focus=ids(library[JULY]))
    assert files(result)[0] == "Q3-Cooling-Audit-Lahore-DC.txt"
    assert JULY not in files(result)


def test_a_follow_up_without_a_topic_borrows_only_the_referenced_turn(client, library):
    """Three turns back is the dashboard; the referenced turn (the latest with documents) is the July bill. Only its
    question is borrowed, and only its documents are searched."""
    history = [
        turn("What is in the billing dashboard capture?", "Outstanding balance $0.00. [S1]", library[DASHBOARD], grounded=True),
        turn("What was the July 2026 internet bill total?", "Rs. 41,250. [S1]", library[JULY], grounded=True),
    ]
    result = chat(USER_EMAIL, "aur?", history=history, focus=ids(library[JULY]))
    assert set(files(result)) == {JULY}


def test_the_evidence_check_reads_a_follow_up_as_resolved(client, library, monkeypatch):
    """A follow-up with no topic of its own is checked against the question it was resolved with."""
    from app.rag import chat as chat_module

    seen = {}
    real = chat_module.answer_question

    async def recording(*args, **kwargs):
        seen["check_question"] = kwargs.get("check_question")
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_module, "answer_question", recording)
    history = [turn("What was the July 2026 internet bill total?", "Rs. 41,250. [S1]", library[JULY], grounded=True)]
    chat(USER_EMAIL, "aur?", history=history, focus=ids(library[JULY]))
    assert "July 2026 internet bill total" in (seen["check_question"] or "")


def test_a_history_without_source_state_keeps_the_previous_behaviour(client, library):
    """Stateless API clients send history without source state: the follow-up still takes the previous question."""
    history = [Turn("What did the Lahore DC cooling audit find?", "Chiller 2 vibrates. [S1]")]
    result = chat(USER_EMAIL, "aur?", history=history)
    assert "Q3-Cooling-Audit-Lahore-DC.txt" in files(result)


def test_tracked_documents_are_reauthorised(client, library):
    """A follow-up never searches a tracked document the user can no longer read."""
    secret = library["Salary-Revision-Confidential.txt"]
    history = [turn("What is in Salary-Revision-Confidential.txt?", "Manager PKR 385,000. [S1]", secret, grounded=True)]
    outsider = chat(USER_EMAIL, "aur?", history=history, focus=ids(secret))
    assert "Salary-Revision-Confidential.txt" not in files(outsider) and "385,000" not in contents(outsider)
    member = chat(USER2_EMAIL, "aur?", history=history, focus=ids(secret))
    assert files(member) == ["Salary-Revision-Confidential.txt"]


def test_a_question_naming_documents_is_never_answered_from_general_knowledge(client, library):
    """Open mode answers general questions from general knowledge, but not one that names a document."""
    result = chat(USER_EMAIL, "Internet-Bills-July2026 ka due date kya hai?", rag_general_answers=True,
                  reply="INSUFFICIENT_EVIDENCE: the memo does not give a due date.")
    assert result.answer.insufficient_evidence and not result.answer.general
    assert result.named_documents == ids(library[JULY])


def test_saved_turns_record_their_source_state(client, fake_provider, library):
    """A saved answer stores the documents its question named; reopening the chat resolves follow-ups with them."""
    from sqlalchemy import select

    from app.db.models import ConversationMessage
    from tests.conftest import run_db
    from tests.test_conversations import new_chat, send

    login(client, USER_EMAIL)
    fake_provider.rag_reply = lambda message: "INSUFFICIENT_EVIDENCE: no amount is stated."
    chat_id = new_chat(client)
    assert send(client, chat_id, "july ka kitna bill tha").status_code == 200
    payloads = run_db(lambda c: c.execute(
        select(ConversationMessage.payload).where(ConversationMessage.role == "assistant")
    )).scalars().all()
    assert payloads[-1]["named_document_ids"] == [library[JULY]]
    body = client.get(f"/api/conversations/{chat_id}").json()
    assert "named_document_ids" not in str(body)  # internal state: never returned by the API
