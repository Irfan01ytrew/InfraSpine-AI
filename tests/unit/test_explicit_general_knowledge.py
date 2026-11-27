import pytest

from app.rag.answer import explicitly_requests_general_knowledge


@pytest.mark.parametrize("question", [
    "Using general knowledge, show a Python addition function and a comparison table.",
    "Use your general knowledge to explain photosynthesis.",
    "Can you answer from general knowledge: what is H2O?",
    "Based on general knowledge, calculate 17 times 23.",
    "General knowledge se Roman Urdu mein photosynthesis samjhao.",
    "عام معلومات سے بتائیں کہ سورج کیا ہے؟",
    "Answer outside my uploaded documents: what is a prime number?",
])
def test_affirmative_general_requests_are_recognized(question):
    assert explicitly_requests_general_knowledge(question)


@pytest.mark.parametrize("question", [
    "Do not use general knowledge; use the refund policy.pdf.",
    "Don't answer from general knowledge. Read the uploaded policy.",
    "Without using general knowledge, summarize policy.pdf.",
    "Use the source instead of using general knowledge.",
    "General knowledge se nahi, file se batao.",
    "عام معلومات سے نہیں بلکہ دستاویز سے جواب دیں۔",
    "Summarize the General Knowledge textbook.pdf.",
    "Does the General Knowledge document mention oxygen?",
])
def test_document_requests_and_negations_do_not_opt_out_of_grounding(question):
    assert not explicitly_requests_general_knowledge(question)
