"""Conservative lexical and bounded-alias triage for contradictory knowledge statements.

This detector only creates a human-review candidate. It does not decide truth, publish knowledge,
change retrieval, or emit a user-facing answer. Reviewers must inspect both source-backed statements.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_NEGATED_CONTRACTION = re.compile(
    r"\b(?:can't|won't|wouldn't|shouldn't|couldn't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|"
    r"hasn't|haven't|hadn't|mustn't|needn't|shan't)\b",
    re.I,
)
_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
)
_NUMBER = re.compile(
    rf"(?<!\w)(\d+(?:[.,]\d+)?|{_NUMBER_WORD}(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)"
    r"(?:\s*(days?|weeks?|months?|years?|hours?|minutes?|percent|%))?(?!\w)",
    re.I,
)
_STOP = {
    "about", "after", "all", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "the", "their", "this", "to", "with", "within", "under", "over", "per",
}
_OPPOSITES = (
    frozenset({"allow", "allows", "allowed", "permit", "permits", "permitted"}),
    frozenset({"ban", "bans", "banned", "disallow", "disallows", "disallowed", "forbid", "forbids", "forbidden", "prohibit", "prohibits", "prohibited"}),
    frozenset({"must", "mandatory", "required", "requires"}),
    frozenset({"may", "optional", "voluntary"}),
    frozenset({"active", "enabled", "open"}),
    frozenset({"inactive", "disabled", "closed"}),
    frozenset({"increase", "increases", "increased", "higher", "more"}),
    frozenset({"decrease", "decreases", "decreased", "lower", "less"}),
    frozenset({"include", "includes", "included"}),
    frozenset({"exclude", "excludes", "excluded"}),
    frozenset({"keep", "keeps", "keeping", "kept", "retain", "retains", "retained", "preserve", "preserves", "preserved", "custody"}),
    frozenset({"delete", "deletes", "deleted", "remove", "removes", "removed", "destroy", "destroys", "destroyed"}),
)
_NEGATION = {"no", "not", "never", "cannot", "cant", "without", "neither", "nor"}
SEMANTIC_CONFLICT_MIN_COSINE = 0.52
_SYNONYM_GROUPS = (
    ("employee", frozenset({"employee", "employees", "staff", "personnel", "worker", "workers"})),
    (
        "company",
        frozenset({"company", "companies", "corporate", "organization", "organizations", "organisation", "organisations"}),
    ),
    (
        "computer",
        frozenset({"laptop", "laptops", "notebook", "notebooks", "computer", "computers", "desktop", "desktops"}),
    ),
    ("backup", frozenset({"backup", "backups", "copy", "copies"})),
    (
        "custody",
        frozenset({
            "keep", "keeps", "keeping", "kept", "retain", "retains", "retained",
            "store", "stores", "storing", "stored", "save", "saves", "saved",
            "preserve", "preserves", "preserved",
        }),
    ),
    (
        "delete",
        frozenset({
            "delete", "deletes", "deleted", "remove", "removes", "removed", "destroy", "destroys", "destroyed",
            "discard", "discards", "discarded", "purge", "purges", "purged", "erase", "erases", "erased",
            "dispose", "disposes", "disposed", "expunge", "expunges", "expunged",
        }),
    ),
)
_SYNONYM_CANONICAL = {
    term: canonical
    for canonical, group in _SYNONYM_GROUPS
    for term in group
}


def _tokens(statement: str) -> set[str]:
    tokens = {token.casefold() for token in _TOKEN.findall(statement) if len(token) > 2 or token.casefold() in _NEGATION}
    if _NEGATED_CONTRACTION.search(statement):
        tokens.add("not")
    return tokens


def conflict_search_query(statement: str) -> str:
    """Build a parameter-safe PostgreSQL tsquery prefilter from normalized word tokens."""
    terms_set = _tokens(statement)
    terms_set.update(token.casefold() for token in _TOKEN.findall(statement) if token.isdigit())
    canonical_groups = {
        group
        for _, group in _SYNONYM_GROUPS
        if terms_set & group
    }
    for group in canonical_groups:
        terms_set.update(group)
    terms = sorted(terms_set - _STOP)
    return " | ".join(f"{term}:*" for term in terms)


def _canonical_tokens(statement: str) -> set[str]:
    return {_SYNONYM_CANONICAL.get(token, token) for token in _tokens(statement)}


_NUMBER_WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_TIME_UNIT_MINUTES = {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "year": 525600}


def _number_value(value: str) -> Decimal:
    words = value.casefold().replace("-", " ").split()
    if words and words[0] in _NUMBER_WORD_VALUES:
        return Decimal(_NUMBER_WORD_VALUES[words[0]] + sum(_NUMBER_WORD_VALUES.get(word, 0) for word in words[1:]))
    numeric = value.replace(",", "")
    try:
        return Decimal(numeric)
    except InvalidOperation:
        return Decimal(0)


def _numbers(statement: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    for match in _NUMBER.finditer(statement):
        amount = _number_value(match.group(1))
        unit = (match.group(2) or "").casefold().rstrip("s")
        if unit == "%":
            unit = "percent"
        if unit in _TIME_UNIT_MINUTES:
            amount *= _TIME_UNIT_MINUTES[unit]
            unit = "minute"
        values.add((format(amount.normalize(), "f"), unit))
    return values


def _has_opposing_terms(left_tokens: set[str], right_tokens: set[str]) -> bool:
    if bool(left_tokens & _NEGATION) != bool(right_tokens & _NEGATION):
        return True
    for opposite_a, opposite_b in zip(_OPPOSITES[::2], _OPPOSITES[1::2]):
        if (left_tokens & opposite_a and right_tokens & opposite_b) or (left_tokens & opposite_b and right_tokens & opposite_a):
            return True
    return False


def potential_conflict(left: str, right: str, *, semantic_similarity: float | None = None) -> bool:
    """Return whether two similarly scoped statements merit human comparison.

    The overlap threshold deliberately errs toward missed candidates over noisy alerts. A small
    controlled alias list catches common employee/company/computer wording differences. Below the
    lexical threshold, local embedding similarity can retrieve a candidate only when its explicit
    polarity is opposed or its normalized measurements differ. A returned candidate always requires
    a human to compare the cited sources; similarity never decides truth or publication.
    """
    left_raw = _tokens(left)
    right_raw = _tokens(right)
    left_tokens = _canonical_tokens(left) - _STOP
    right_tokens = _canonical_tokens(right) - _STOP
    shared = left_tokens & right_tokens
    overlap = len(shared) / max(1, min(len(left_tokens), len(right_tokens)))
    shared_alias_groups = sum(
        1
        for _, group in _SYNONYM_GROUPS
        if left_raw & group and right_raw & group and (left_raw & group) != (right_raw & group)
    )
    strong_polarity_conflict = _has_opposing_terms(left_tokens, right_tokens)
    strong_alias_match = len(shared) >= 3 and shared_alias_groups >= 2 and strong_polarity_conflict
    lexical_candidate = (len(shared) >= 2 and overlap >= 0.55) or strong_alias_match
    semantic_candidate = (
        semantic_similarity is not None
        and semantic_similarity >= SEMANTIC_CONFLICT_MIN_COSINE
        and (strong_polarity_conflict or (_numbers(left) and _numbers(right) and _numbers(left) != _numbers(right)))
    )
    if not lexical_candidate and not semantic_candidate:
        return False

    left_numbers = _numbers(left)
    right_numbers = _numbers(right)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return True

    return strong_polarity_conflict
