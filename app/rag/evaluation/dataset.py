"""Golden retrieval cases: question, expected document, passage, language and persona.

A dataset is portable. It names permission personas (`owner`, `outsider`, …) and does not
contain account emails. The deployment supplies those emails in a separate persona map that
stays under `data/evaluation/`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

LANGUAGES = ("en", "ur", "roman_ur", "mixed")
# Answer-evaluation categories. Retrieval datasets omit `category` and stay valid.
CATEGORIES = (
    "answerable",
    "similar_insufficient",
    "absent",
    "unauthorized",
    "conflicting",
    "longer",
)
_ANSWER_CATEGORIES = frozenset({"answerable", "conflicting", "longer"})
_REFUSAL_CATEGORIES = frozenset({"similar_insufficient", "absent", "unauthorized"})
_PERSONA = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class EvaluationError(ValueError):
    """The dataset, persona map, or report path cannot be used."""


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    language: str
    persona: str
    expect_abstain: bool
    expected_document: str | None = None
    expected_passage: str | None = None
    category: str | None = None
    expected_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    conflict_markers: tuple[str, ...] = ()
    expect_refusal: bool = False
    # Earlier turns of the conversation, oldest first: (question, answer). An answer of None is a turn the
    # documents could not answer, which is how follow-ups like "?" or "why?" are tested.
    history: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class Dataset:
    name: str
    cases: tuple[EvalCase, ...]
    sha256: str
    description: str = ""


def load_dataset(path: Path) -> Dataset:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise EvaluationError("A dataset file must be a JSON object.")
    unknown = set(raw) - {"name", "description", "cases"}
    if unknown:
        raise EvaluationError(f"Unknown dataset fields: {', '.join(sorted(unknown))}.")
    name = _short_text(raw.get("name"), "Dataset name", 80)
    if not name.replace("-", "").replace("_", "").isalnum():
        raise EvaluationError("Dataset name must be letters, digits, hyphens or underscores.")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        raise EvaluationError("Dataset description must be a string of at most 500 characters.")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw or len(cases_raw) > 5000:
        raise EvaluationError("A dataset needs between 1 and 5000 cases.")
    seen: set[str] = set()
    cases: list[EvalCase] = []
    for index, item in enumerate(cases_raw, start=1):
        case = _case(item, index)
        if case.id in seen:
            raise EvaluationError(f"Duplicate case id {case.id!r}.")
        seen.add(case.id)
        cases.append(case)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return Dataset(name=name, description=description, cases=tuple(cases), sha256=digest)


def load_personas(path: Path) -> dict[str, str]:
    raw = _read_json(path)
    if not isinstance(raw, dict) or not raw:
        raise EvaluationError("A persona map must be a non-empty JSON object of persona names to emails.")
    personas: dict[str, str] = {}
    for name, email in raw.items():
        if not isinstance(name, str) or _PERSONA.fullmatch(name) is None:
            raise EvaluationError(f"Persona name {name!r} must be a short lowercase identifier.")
        if not isinstance(email, str) or not email.strip() or len(email) > 320:
            raise EvaluationError(f"Persona {name!r} needs an email address.")
        personas[name] = email.strip().lower()
    return personas


def _case(item: object, index: int) -> EvalCase:
    if not isinstance(item, dict):
        raise EvaluationError(f"Case {index} must be a JSON object.")
    unknown = set(item) - {
        "id", "question", "expected_document", "expected_passage", "language", "persona", "expect_abstain",
        "category", "expected_facts", "forbidden_facts", "conflict_markers", "expect_refusal", "history",
    }
    if unknown:
        raise EvaluationError(f"Case {index} has unknown fields: {', '.join(sorted(unknown))}.")
    case_id = _short_text(item.get("id"), f"Case {index} id", 80)
    if not case_id.replace("-", "").replace("_", "").isalnum():
        raise EvaluationError(f"Case id {case_id!r} must be letters, digits, hyphens or underscores.")
    question = _short_text(item.get("question"), f"Case {case_id} question", 2000)
    language = item.get("language")
    if language not in LANGUAGES:
        raise EvaluationError(f"Case {case_id} language must be one of: {', '.join(LANGUAGES)}.")
    persona = item.get("persona")
    if not isinstance(persona, str) or _PERSONA.fullmatch(persona) is None:
        raise EvaluationError(f"Case {case_id} persona must be a short lowercase identifier.")
    expect_abstain = item.get("expect_abstain", False)
    if not isinstance(expect_abstain, bool):
        raise EvaluationError(f"Case {case_id} expect_abstain must be true or false.")
    expect_refusal = item.get("expect_refusal", False)
    if not isinstance(expect_refusal, bool):
        raise EvaluationError(f"Case {case_id} expect_refusal must be true or false.")
    category = item.get("category")
    if category is not None and category not in CATEGORIES:
        raise EvaluationError(f"Case {case_id} category must be one of: {', '.join(CATEGORIES)}.")
    document = _optional_text(item.get("expected_document", None), f"Case {case_id} expected_document", 255)
    passage = _optional_text(item.get("expected_passage", None), f"Case {case_id} expected_passage", 2000)
    facts = _string_list(item.get("expected_facts"), f"Case {case_id} expected_facts")
    forbidden = _string_list(item.get("forbidden_facts"), f"Case {case_id} forbidden_facts")
    markers = _string_list(item.get("conflict_markers"), f"Case {case_id} conflict_markers")
    history = _history(item.get("history"), case_id)
    if category is None:
        if not expect_abstain and not document and not passage:
            raise EvaluationError(f"Case {case_id} needs an expected document or passage, or expect_abstain.")
    else:
        _check_answer_case(case_id, category, expect_refusal, document, passage, facts, forbidden)
    return EvalCase(
        id=case_id,
        question=question,
        language=language,
        persona=persona,
        expect_abstain=expect_abstain,
        expected_document=document,
        expected_passage=passage,
        category=category,
        expected_facts=facts,
        forbidden_facts=forbidden,
        conflict_markers=markers,
        expect_refusal=expect_refusal,
        history=history,
    )


def _history(value: object, case_id: str) -> tuple[tuple[str, str | None], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 5:
        raise EvaluationError(f"Case {case_id} history must be a list of at most 5 turns.")
    turns = []
    for turn in value:
        if not isinstance(turn, dict) or set(turn) - {"question", "answer"} or "question" not in turn:
            raise EvaluationError(f"Case {case_id} history turns need a question and an optional answer.")
        question = _short_text(turn["question"], f"Case {case_id} history question", 2000)
        answer = turn.get("answer")
        if answer is not None:
            answer = _short_text(answer, f"Case {case_id} history answer", 4000)
        turns.append((question, answer))
    return tuple(turns)


def _check_answer_case(
    case_id: str,
    category: str,
    expect_refusal: bool,
    document: str | None,
    passage: str | None,
    facts: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    """Answer labels must agree with the category so a case cannot ask for both an answer and a refusal."""
    if category in _ANSWER_CATEGORIES:
        if expect_refusal:
            raise EvaluationError(f"Case {case_id} category {category} cannot expect a refusal.")
        if not facts:
            raise EvaluationError(f"Case {case_id} needs expected_facts.")
        if not document and not passage:
            raise EvaluationError(f"Case {case_id} needs an expected document or passage.")
        if category == "conflicting" and not forbidden:
            raise EvaluationError(f"Case {case_id} needs forbidden_facts for the outdated value.")
        return
    if not expect_refusal:
        raise EvaluationError(f"Case {case_id} category {category} must expect a refusal.")
    if category == "unauthorized" and not forbidden:
        raise EvaluationError(f"Case {case_id} needs forbidden_facts for the restricted value.")
    if category == "unauthorized" and not document and not passage:
        raise EvaluationError(f"Case {case_id} needs the restricted document or passage so a leak can be detected.")


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 8:
        raise EvaluationError(f"{label} must be a list of at most 8 strings.")
    return tuple(_short_text(item, label, 200) for item in value)


def _optional_text(value: object, label: str, limit: int) -> str | None:
    if value is None:
        return None
    text = _short_text(value, label, limit)
    return text or None


def _short_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise EvaluationError(f"{label} must be a string.")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise EvaluationError(f"{label} must be 1 to {limit} characters.")
    return text


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise EvaluationError(f"File not found: {path.name}.") from None
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"{path.name} is not valid JSON ({exc.lineno}:{exc.colno}).") from None
