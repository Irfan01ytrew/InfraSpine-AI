"""Pronoun-only follow-ups must borrow the conversation's topic instead of becoming public web lookups."""

import pytest

from app.rag.answer import is_contextual_followup, is_web_lookup_request


@pytest.mark.parametrize("question", ["where are most of them?", "what are the challanges in it?", "where is he now?"])
def test_pronoun_only_follow_ups_are_contextual_and_not_public_location_lookups(question):
    assert is_contextual_followup(question)
    assert not is_web_lookup_request(question)


@pytest.mark.parametrize("question", ["where is the ISS right now?", "Where is Imran Khan?"])
def test_named_live_location_questions_still_use_the_web(question):
    assert is_web_lookup_request(question)
    assert not is_contextual_followup(question)


@pytest.mark.parametrize("question", ["What is new in IT policy", "what is in it for me", "write a function that removes duplicates"])
def test_ordinary_standalone_questions_do_not_become_follow_ups(question):
    assert not is_contextual_followup(question)
