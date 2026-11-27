"""Questions about every document at once, and broad questions about the document being discussed."""

import pytest

from app.rag.answer import GENERAL_SYSTEM_PROMPT, is_library_question, is_overview_question
from tests.conftest import USER_EMAIL, login
from tests.rag_helpers import activate_provider, ask, make_pdf, process_all, upload

CCTV = "CCTV summary: 14 cameras are offline and need inspection. The FSD office has no audio-enabled cameras."
AUDIT = "Access audit: Nida Shafiq viewed report Form-1 2055 times between 5 and 19 September."


@pytest.mark.parametrize("text", ["check all doc", "summarize all the documents", "saare documents check karo",
                                  "documents sab dekho", "what is common across my files?"])
def test_library_questions_are_detected(text):
    assert is_library_question(text)


@pytest.mark.parametrize("text", ["what is things I need to pay attention", "what are the action items?",
                                  "what should I focus on", "important points batao"])
def test_broad_questions_read_the_whole_document(text):
    assert is_overview_question(text)


@pytest.mark.parametrize("text", ["what is issue in misu cameras?", "check which access she most", "all good?"])
def test_specific_questions_stay_on_normal_retrieval(text):
    assert not is_library_question(text) and not is_overview_question(text)


def test_general_answers_never_claim_the_library_is_empty():
    prompt = " ".join(GENERAL_SYSTEM_PROMPT.split())  # the prompt is wrapped across lines
    assert "Never claim that the document library is empty or unavailable" in prompt
    assert "Never claim you cannot access uploaded files" in prompt


def test_check_all_documents_reads_every_readable_document(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "cctv.pdf", make_pdf(CCTV))
    upload(client, "audit.pdf", make_pdf(AUDIT))
    process_all(client)
    seen = []

    def reply(message: str) -> str:
        seen.append(message)
        return "14 cameras are offline [S1]. Form-1 was viewed 2055 times [S2]."

    fake_provider.rag_reply = reply
    body = ask(client, "check all doc").json()
    assert body["strategy"] == "library_overview" and body["insufficient_evidence"] is False
    assert {c["filename"] for c in body["citations"]} == {"cctv.pdf", "audit.pdf"}
    assert "14 cameras" in seen[0] and "Form-1" in seen[0]  # both documents were sent, whatever their similarity
