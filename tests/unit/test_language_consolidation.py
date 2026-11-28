"""Step Z slice 2: the three ad-hoc Roman Urdu copies now live in app/language.

Behaviour-preservation pins. Every expectation below was produced by the
pre-consolidation code (verified by a differential run of the old inline
regexes / old modules against the shared helper over a 60-question corpus,
0 differences), so these tests fail if consolidation changes routing, reply
language or search queries.

Merge notes: Agent 12's knowledge-fix branch changes
``is_web_lookup_request`` and the ``'pak next kb'`` query (``'kb'`` becomes a
stopword there), and Agent 20's live-data branch changes the cricket
next-match routing. Both merged at integration (Agent 161 repair commit):
the expectations below were updated to the merged behaviour — ``'kb'`` is
a stopword, the query is ``'pak next match'``, and both next-match
questions route to web lookup.
"""

import pytest

from app.language import contains_roman_urdu_words, contains_urdu_script
from app.language.words import (
    ROMAN_URDU_CONTENT_WORDS,
    ROMAN_URDU_INVENTORY_WORDS,
    ROMAN_URDU_MEDIA_WORDS,
    ROMAN_URDU_QUERY_NOISE_WORDS,
    ROMAN_URDU_QUERY_STOPWORDS,
)
from app.rag.answer import is_web_lookup_request, normalized_live_location_query
from app.services.public_web import (
    _CURRENT_NEWS_REQUEST,
    _QUERY_STOP_WORDS,
    _SEARCH_QUERY_NOISE,
    _public_search_queries,
    _query_terms,
)


# --- chat.py word sets: the three sites keep their own exact lists --------

@pytest.mark.parametrize(
    "question, media, inventory, content",
    [
        # All three lists share kya/hai.
        ("yeh file kya hai", True, True, True),
        # "aap" is only in the media list.
        ("aap video dekhein", True, False, False),
        # "saal"/"dikhao" are in no chat list: still English everywhere.
        ("saal 2025 ke documents dikhao", False, False, False),
        # "kitne" is only in the inventory list (plus shared "hain").
        ("kitne documents hain", True, True, True),
        # "mere" is only in the inventory list.
        ("mere documents", False, True, False),
        # "aaj" is in no chat list; "kitne" carries the inventory one.
        ("aaj kitne documents", False, True, False),
        # Mixed script: the media site sees Urdu script; the others see "kya".
        ("یہ file kya hai", True, True, True),
        # A lone Urdu-script digit counts as script for the media site only.
        ("۵", True, False, False),
        ("hello world", False, False, False),
        # "ab"/"tk" are inventory-only.
        ("ab tk kitne", False, True, False),
        # "batao" is in the media and content lists, not inventory.
        ("batao", True, False, True),
        # "rha" is media-only.
        ("rha hai", True, True, True),
    ],
)
def test_chat_word_sets_keep_their_exact_lists(question, media, inventory, content):
    assert (
        contains_urdu_script(question)
        or contains_roman_urdu_words(question, ROMAN_URDU_MEDIA_WORDS)
    ) == media
    assert contains_roman_urdu_words(question, ROMAN_URDU_INVENTORY_WORDS) == inventory
    assert contains_roman_urdu_words(question, ROMAN_URDU_CONTENT_WORDS) == content


def test_chat_word_sets_are_the_single_home_for_the_lists():
    # The two identical video/image copies are now one set.
    assert ROMAN_URDU_CONTENT_WORDS == {
        "kya", "kia", "kiya", "batao", "btao", "mein", "mai", "hain", "hai",
    }
    # Spot-check the other two sets keep their exact words.
    assert ROMAN_URDU_MEDIA_WORDS == {
        "kya", "kia", "kiya", "btao", "batao", "rha", "rhi", "mein", "mai",
        "hain", "hai", "aap", "tum", "mujhe", "mujh", "iss", "yeh",
    }
    assert ROMAN_URDU_INVENTORY_WORDS == {
        "kitna", "kitni", "kitne", "ktna", "ktni", "ktne", "kya", "kia",
        "kiya", "meri", "mere", "mai", "ab", "tk", "hain", "hai",
    }


# --- public_web.py: stopwords / noise sets are unchanged unions -------------

def test_public_web_stopwords_unchanged():
    assert ROMAN_URDU_QUERY_STOPWORDS <= _QUERY_STOP_WORDS
    assert ROMAN_URDU_QUERY_NOISE_WORDS <= _SEARCH_QUERY_NOISE
    # The Roman Urdu blocks are the only words that moved: every stopword is
    # either an original English word or a shared Roman Urdu word.
    assert "kb" in _QUERY_STOP_WORDS  # knowledge-fix interrogatives merged into ROMAN_URDU_QUERY_STOPWORDS at integration
    assert "batao" in _QUERY_STOP_WORDS and "batao" in _SEARCH_QUERY_NOISE
    assert "dhoondo" in _QUERY_STOP_WORDS and "dhoondo" in _SEARCH_QUERY_NOISE


def test_public_search_query_unchanged():
    # knowledge-fix (merged at integration): "kb" is a stopword now.
    assert _public_search_queries("pak a ka next match kb hai?") == ["pak next match"]
    assert _public_search_queries("mujhe aaj ka dollar rate batao") == ["dollar rate"]
    assert _query_terms("pak a ka next match kb hai?") == {"pak", "next", "match"}


def test_current_news_request_still_matches_roman_urdu():
    assert _requires_current_news("aaj ki taaza khabar kya hai")
    assert _requires_current_news("taaza khabar batao")
    assert not _requires_current_news("khabar ka matlab kya hai")


def _requires_current_news(question: str) -> bool:
    return bool(_CURRENT_NEWS_REQUEST.search(question))


# --- answer.py: web-lookup routing unchanged --------------------------------

@pytest.mark.parametrize(
    "question, expected",
    [
        ("Lahore kahan hai?", True),
        ("Karachi kidhar hai", True),
        ("where is Lahore?", True),
        ("where is my file?", False),
        ("meri file kahan hai", False),
        ("aaj ka dollar rate kya hai", True),
        ("aaj ki news batao", True),
        ("cricket match ka score kya hai", True),
        ("pakistan ne match jeeta?", True),
        ("kon jeeta aaj ka match?", True),
        ("آج کا میچ کون جیتا؟", True),
        ("kya aap ke paas live web search hai", False),
        ("internet se pakistan ki latest news batao", True),
        ("mere documents mein invoice dikhao", False),
        ("aaj ka match kis waqt hai", True),
        ("download the report", False),
        # knowledge-fix + live-data (merged at integration) route both to True.
        ("pak a ka next match kb hai?", True),
        ("pakistan ka agla match kab hai", True),
    ],
)
def test_web_lookup_routing_unchanged(question, expected):
    assert is_web_lookup_request(question) == expected


def test_live_location_query_unchanged():
    assert normalized_live_location_query("Lahore kahan hai?") == "Lahore current location"
    assert normalized_live_location_query("where is Lahore?") == "Lahore current location"
    assert normalized_live_location_query("where is my file?") is None
    assert normalized_live_location_query("meri file kahan hai") is None
