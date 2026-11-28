"""Shared language helpers for InfraSpine.

Pure functions only: no imports from ``app.platform``, no DB, no network.
Language detection never changes authorization, permissions, approvals, or
what a user may read — it only changes how text is understood and shown.
Document / web / message text stays untrusted whatever language it is in.
"""

from .detect import (
    contains_roman_urdu_words,
    contains_urdu_script,
    detect_language,
    is_roman_urdu,
)
from .normalize import (
    normalize_command_words,
    normalize_date_words,
    normalize_digits,
    normalize_for_matching,
    normalize_number_words,
    roman_urdu_to_urdu_script,
    urdu_script_to_roman,
)

__all__ = [
    "contains_roman_urdu_words",
    "contains_urdu_script",
    "detect_language",
    "is_roman_urdu",
    "normalize_command_words",
    "normalize_date_words",
    "normalize_digits",
    "normalize_for_matching",
    "normalize_number_words",
    "roman_urdu_to_urdu_script",
    "urdu_script_to_roman",
]
