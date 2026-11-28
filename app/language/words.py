"""Consolidated Roman Urdu / Urdu-script vocabularies (Step Z slice 2).

Single home for every Roman Urdu word list that used to be copy-pasted
across ``app/rag/chat.py``, ``app/rag/answer.py`` and
``app/services/public_web.py``.

Each set preserves the EXACT word list of the ad-hoc copy it replaces —
same words, same behaviour; only the location changed. Do not add words to
a set to "improve" detection: that would silently change routing, reply
language or search queries. New vocabulary belongs in
``app/language/detect.py``'s ``ROMAN_URDU_WORDS`` (detection) or in a new
set with its own tests.

Two shapes, for two uses:

- ``frozenset`` — membership tests (stopwords, query noise) and the
  ``contains_roman_urdu_words`` word search in ``app/language/detect.py``.
  Order is irrelevant there; the boolean result is identical to the old
  inline regexes (verified by ``tests/unit/test_language_consolidation.py``).
- ``tuple`` — ordered alternation fragments for the routing regexes in
  ``app/rag/answer.py`` / ``app/services/public_web.py``. The tuples keep
  the original alternation order, so the regexes built from them compile
  to byte-identical patterns.
"""

from __future__ import annotations

# --- app/rag/chat.py -------------------------------------------------------
# Which question words make a reply come back in Roman Urdu. Three sites,
# three (slightly different) lists — kept separate on purpose so behaviour
# stays identical; only the duplicated 1293/1417 copy became one set.

# Media fallback intro (was chat.py ~line 343).
ROMAN_URDU_MEDIA_WORDS = frozenset(
    {
        "kya", "kia", "kiya", "btao", "batao", "rha", "rhi", "mein", "mai",
        "hain", "hai", "aap", "tum", "mujhe", "mujh", "iss", "yeh",
    }
)

# Library-inventory intro (was chat.py ~line 558).
ROMAN_URDU_INVENTORY_WORDS = frozenset(
    {
        "kitna", "kitni", "kitne", "ktna", "ktni", "ktne", "kya", "kia",
        "kiya", "meri", "mere", "mai", "ab", "tk", "hain", "hai",
    }
)

# Video / image "not indexed" intro (was chat.py ~lines 1293 and 1417 —
# the same list twice; now one set).
ROMAN_URDU_CONTENT_WORDS = frozenset(
    {"kya", "kia", "kiya", "batao", "btao", "mein", "mai", "hain", "hai"}
)

# --- app/services/public_web.py --------------------------------------------
# Roman Urdu / Hindi request words stripped from public search queries
# (was the trailing block of _QUERY_STOP_WORDS, verbatim).
ROMAN_URDU_QUERY_STOPWORDS = frozenset(
    {
        "acha", "achha", "aj", "aaj", "batao", "btao", "batayein", "btaein",
        "batana", "chahiye", "chahye", "dekho", "dekhna", "dhoondo",
        "dhondo", "dhundho", "dhoondho", "hai", "hain", "he", "h", "ka",
        "ke", "ki", "karo", "kro", "karna", "krna", "kya", "kia", "konsi",
        "konse", "konsa", "konsay", "koun", "kounsa", "kounsi", "mujhe",
        "mujhay", "mujai", "meri", "mere", "mera", "mein", "mai", "me",
        "par", "pe", "please", "se", "sy", "to", "ye", "yeh", "iss", "is",
        "kiya", "jao", "jayein",
        # Knowledge-fix interrogatives (Agent 12), merged at integration:
        # without these, "pak a ka next match kb hai?" produced the nonsense
        # query "pak next kb".
        "kb", "kab", "kub", "kahan", "kha", "kidhar", "kidr", "kidher",
        "kis", "kisko", "kon", "kaun", "kaunsa", "kaunsi", "kaunse",
        "kyun", "kyu", "kaise", "kaisa", "kaisi", "kese", "kabhi",
        "kitna", "kitni", "kitne", "kitnay", "kahaan",
    }
)

# Roman Urdu words treated as query noise (was the trailing block of
# _SEARCH_QUERY_NOISE, verbatim).
ROMAN_URDU_QUERY_NOISE_WORDS = frozenset(
    {
        "acha", "achha", "batao", "btao", "batayein", "dekho", "dhoondo",
        "dhondo", "hai", "hain", "ka", "ke", "ki", "karo", "kro", "kya",
        "kia", "mein", "mai", "par", "pe", "se", "sy",
    }
)

# --- app/rag/answer.py + app/services/public_web.py -------------------------
# Ordered fragments for the routing regexes. Tuples keep the original
# alternation order so the rebuilt patterns are byte-identical.

# "Search the web" verbs, as stems (the regexes match dhond/dhoondo/...).
ROMAN_URDU_SEARCH_STEMS = ("dhond", "dhoond", "talash")

# "Tell me" verbs in a web request ("internet se batao ...").
ROMAN_URDU_TELL_WORDS = ("batao", "btao", "batayein", "samjhao")

# "Find out / look / open" verbs in a web request.
ROMAN_URDU_KNOWLEDGE_VERBS = (
    "maloom", "pata", "jaankari", "jankari", "dekho", "dekhna",
    "parho", "parhna", "khol", "kholo", "jao",
)

# "Look at the site" verbs in the site-visit clause. The clause's head
# ("jao|kholo|open|visit|check") stays in app/rag/answer.py because it
# interleaves Roman Urdu and English; "jao"/"kholo" also live in
# ROMAN_URDU_KNOWLEDGE_VERBS above.
ROMAN_URDU_SITE_VERBS = ("dekho", "dekhna", "parho", "parhna")

# Prepositions after "internet / web / online" ("internet se search karo").
ROMAN_URDU_WEB_PREPOSITIONS = ("se", "sy", "par", "per", "mein", "me")

# Today / right-now words ("aaj ka rate", "aaj ki news").
ROMAN_URDU_TODAY_WORDS = ("aaj", "aj", "abhi")

# "Where is X?" question words ("Lahore kahan hai?").
ROMAN_URDU_LOCATION_WORDS = ("kahan", "kha", "kidhar", "kidr")

# Copula after a location word ("kahan hai / hain / he / h").
ROMAN_URDU_COPULA_WORDS = ("hai", "hain", "he", "h")

# Genitive after a today word ("aaj ka rate", "aaj ki news").
ROMAN_URDU_GENITIVE_WORDS = ("ka", "ki")

# Live / future indicator words in the cricket routing clause
# ("pak a ka next match kb hai?"). The knowledge-fix words (kb/kab, next,
# agla..., schedule/fixture...) were merged in at integration; without them
# routing missed Roman Urdu future questions.
ROMAN_URDU_LIVE_WORDS = (
    "aaj", "aj", "abhi", "kal", "jeeta", "jeetae",
    "kb", "kab", "kub", "next", "agla", "agli", "agle", "agley",
    "upcoming", "schedule", "fixture", "fixtures", "series",
    "kis\\s+se", "kis\\s+ke\\s+sath", "kahan", "kidhar", "kidr",
    "venue", "ground", "stadium",
)

# Freshness words in the current-news request ("taaza khabar").
ROMAN_URDU_FRESH_WORDS = ("taaza", "taza")

# News words in the current-news request ("khabar", "khabrain").
ROMAN_URDU_NEWS_WORDS = ("khabar", "khabrain")

# Urdu-script live words in the cricket routing clause.
URDU_SCRIPT_LIVE_WORDS = ("آج", "اسکور", "سکور", "جیتا", "کب", "اگلا", "اگلی", "آئندہ")  # knowledge-fix words merged at integration


def word_alternation(words) -> str:
    """``"a|b|c"`` for an ordered tuple of literal words."""
    return "|".join(words)


def stem_alternation(stems) -> str:
    """``"dhond\\w*|dhoond\\w*|..."`` for search-verb stems."""
    return "|".join(f"{stem}\\w*" for stem in stems)
