"""Detect the language of a short user text.

Languages: ``"en"`` (English), ``"roman_urdu"`` (Urdu written in Latin
letters — how the owner writes), ``"urdu"`` (Urdu script), ``"mixed"``
(both scripts / both vocabularies in one text), ``"unknown"`` (empty text,
or text with no letters at all, e.g. only digits or punctuation).

Heuristic, not a classifier: one distinctive Roman Urdu word is enough to
call short chat text Roman Urdu, matching the inline regexes this replaces
(``app/rag/chat.py``). Urdu-script digits (``۵``) are neutral — they do not
by themselves make a text Urdu; only Urdu-script *letters* do.
"""

from __future__ import annotations

import re

# Broad range, same convention as app/rag/chat.py (includes digits and
# punctuation in the Arabic block).
URDU_SCRIPT_RE = re.compile(r"[\u0600-\u06ff]")

# Urdu-script letters only (no digits, no punctuation) — used for detection
# so that a Latin sentence containing an Urdu digit stays English.
URDU_LETTER_RE = re.compile(r"[\u0621-\u064a\u066e-\u06d3\u06fa-\u06fc\u06ff]")

LATIN_LETTER_RE = re.compile(r"[A-Za-z]")

# Distinctive Roman Urdu words. Deliberately excludes words that are also
# common English ("do", "is", "main", "to", "so") — those are handled by the
# number/command normalizers, not by detection.
ROMAN_URDU_WORDS = frozenset(
    {
        "aaj", "aap", "ab", "abhi", "agla", "agle", "agli", "aur", "bataen",
        "batao", "bhi", "btao", "chahiye", "crore", "dikhaen", "dikhao",
        "din", "hafte", "hafta", "hai", "hain", "hazaar", "iss", "jeeta",
        "jeetay", "kab", "kahan", "kaise", "kaisa", "kal", "karo",
        "kb", "kia", "kidhar", "kitna", "kitne", "kitni", "kiya", "kon",
        "ktna", "ktne", "ktni", "kub", "kya", "kyun", "kaun", "lakh",
        "mahina", "mahine", "mai", "mein", "mera", "mere", "meri", "mujh",
        "mujhe", "mujai", "nahi", "nahin", "parson", "phir", "pichla",
        "pichle", "pichli", "raha", "rahi", "rha", "rhi", "saal", "sau",
        "tak", "tk", "toh", "tou", "tum", "yeh",
    }
)

ROMAN_URDU_RE = re.compile(
    r"\b(?:" + "|".join(sorted(ROMAN_URDU_WORDS)) + r")\b", re.IGNORECASE
)

# Cache of whole-word patterns per word set, so the consolidated call sites
# in app/rag/chat.py (Step Z slice 2) do not recompile on every question.
_WORDS_RE_CACHE: dict[frozenset, re.Pattern] = {}


def contains_roman_urdu_words(text: str, words: frozenset = ROMAN_URDU_WORDS) -> bool:
    """True when *text* contains any whole word from *words* (case-insensitive).

    Unlike :func:`is_roman_urdu` this does NOT exclude Urdu-script text:
    a mixed question such as "یہ file kya hai" still contains the Roman
    Urdu word ``kya``. That matches the old inline regexes in
    ``app/rag/chat.py`` exactly — pass the site's original word set
    (``app/language/words.py``) to preserve its behaviour.
    """
    if not text:
        return False
    pattern = _WORDS_RE_CACHE.get(words)
    if pattern is None:
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(word) for word in sorted(words)) + r")\b",
            re.IGNORECASE,
        )
        _WORDS_RE_CACHE[words] = pattern
    return bool(pattern.search(text))


def contains_urdu_script(text: str) -> bool:
    """True when *text* contains any character from the Arabic block."""
    return bool(text) and bool(URDU_SCRIPT_RE.search(text))


def is_roman_urdu(text: str) -> bool:
    """True when *text* has Roman Urdu words and no Urdu-script letters."""
    if not text or URDU_LETTER_RE.search(text):
        return False
    return bool(ROMAN_URDU_RE.search(text))


def detect_language(text: str) -> str:
    """Detect ``"en"`` / ``"roman_urdu"`` / ``"urdu"`` / ``"mixed"`` / ``"unknown"``."""
    if not text or not text.strip():
        return "unknown"
    has_urdu_letters = bool(URDU_LETTER_RE.search(text))
    has_latin = bool(LATIN_LETTER_RE.search(text))
    if has_urdu_letters and has_latin:
        return "mixed"
    if has_urdu_letters:
        return "urdu"
    if ROMAN_URDU_RE.search(text):
        return "roman_urdu"
    if has_latin:
        return "en"
    return "unknown"
