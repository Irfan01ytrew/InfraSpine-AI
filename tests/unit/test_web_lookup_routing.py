"""Live/future sport questions must reach public web search instead of dying at the document evidence gate.

Regression: "pak a ka next match kb hai?" (the user's exact question) used to return
"Insufficient document evidence" because the sport clause in is_web_lookup_request
had no "when" word (kb/kab) and no "next/upcoming/schedule", so it never routed to
the web. No uploaded document can answer a future fixture, so these questions must
go to live web search.
"""

import pytest

from app.rag.answer import is_web_lookup_request


@pytest.mark.parametrize(
    "question",
    [
        "pak a ka next match kb hai?",
        "pakistan ka agla match kab hai?",
        "pakistan cricket team ka next match kab hai?",
        "pak ka next match kis se hai?",
        "pakistan ka match kab hai?",
        "pak ka agla match kahan hai?",
        "india vs pakistan next match schedule?",
        "psl ka next match kab hai?",
        "pakistan ka upcoming match kab hai?",
        "pakistan ka next t20 kab hai?",
        "پاکستان کا اگلا میچ کب ہے؟",
        # Previously working routes must keep working.
        "is waqt cricket ke matches live chal rahe hain?",
        "aaj pakistan ka match kis time hai?",
        "What are the latest matches?",
    ],
)
def test_live_or_future_sport_questions_route_to_web(question):
    assert is_web_lookup_request(question)


@pytest.mark.parametrize(
    "question",
    [
        # A named file is about that document, not a public fixture.
        "what is the next match in refund-policy.pdf?",
        "pak ka next match kb hai? meri file mein dekho",
        # Company records stay on permission-filtered RAG.
        "when is the next match in our invoice report?",
        # "match" as a verb over the user's uploads stays on RAG.
        "match the following items in my uploaded files",
        # A bare score word with no sport noun is not a live lookup.
        "what is the score in the sales report?",
        # Pronoun-only follow-ups stay on conversation context.
        "where are most of them?",
    ],
)
def test_document_or_company_match_questions_do_not_route_to_web(question):
    assert not is_web_lookup_request(question)
