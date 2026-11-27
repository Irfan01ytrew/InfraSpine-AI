"""Grounded employee-training prompts built on the regular ACL-filtered RAG answer path."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from app.schemas.rag import Citation, TrainingQuizQuestion


QUIZ_PROMPT = """Create a source-grounded multiple-choice quiz as JSON only. Use this exact shape:
{"questions":[{"question":"...","choices":["...","...","...","..."],"correct_index":0,"explanation":"... [S1]","sources":["S1"]}]}
Write 3 to 5 questions. Each question must have exactly four plausible choices, one correct choice, a brief
explanation, and one or more source IDs. Cite every explanation with literal [S#] tags matching the retrieved
documents. Use only facts explicitly present in those documents. If the sources cannot support three questions,
return {"questions":[]} . Do not include markdown, prose, or answer keys outside this JSON object."""


def parse_training_quiz(
    answer: str, citations: list[Citation]
) -> tuple[list[TrainingQuizQuestion], list[dict[str, object]]]:
    """Fail closed on malformed, uncited, or unsupported generated assessment content.

    The returned answer key is kept in a separate database column and is never part of the plan response.
    """
    try:
        payload = json.loads(answer)
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(payload, dict) or set(payload) != {"questions"}:
        return [], []
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not 3 <= len(raw_questions) <= 5:
        return [], []

    citations_by_id = {citation.source_id: citation for citation in citations}
    public_questions: list[TrainingQuizQuestion] = []
    answer_key: list[dict[str, object]] = []
    for raw in raw_questions:
        if not isinstance(raw, dict) or set(raw) != {
            "question", "choices", "correct_index", "explanation", "sources"
        }:
            return [], []
        question = raw.get("question")
        choices = raw.get("choices")
        correct_index = raw.get("correct_index")
        explanation = raw.get("explanation")
        source_ids = raw.get("sources")
        if (
            not isinstance(question, str)
            or not 5 <= len(question.strip()) <= 600
            or not isinstance(choices, list)
            or len(choices) != 4
            or any(not isinstance(choice, str) or not 1 <= len(choice.strip()) <= 300 for choice in choices)
            or len({choice.strip().casefold() for choice in choices}) != 4
            or not isinstance(correct_index, int)
            or isinstance(correct_index, bool)
            or not 0 <= correct_index < len(choices)
            or not isinstance(explanation, str)
            or not 1 <= len(explanation.strip()) <= 1000
            or not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= 5
            or any(not isinstance(source_id, str) or source_id not in citations_by_id for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            return [], []
        if not any(re.search(rf"\[{re.escape(source_id)}\]", explanation) for source_id in source_ids):
            return [], []
        public_questions.append(
            TrainingQuizQuestion(
                question=question.strip(),
                choices=[choice.strip() for choice in choices],
                citations=[citations_by_id[source_id] for source_id in source_ids],
            )
        )
        answer_key.append({"correct_index": correct_index, "explanation": explanation.strip()})
    return public_questions, answer_key


def training_sections(topic: str) -> list[tuple[str, str]]:
    """Build a small, predictable learning package; the model supplies only evidence-backed content."""
    topic = " ".join(topic.split())
    return [
        (
            "Learning objectives and essentials",
            f"For employee training about {topic}, explain the core concepts and terminology stated in the documents. "
            "Give 3 to 5 practical learning objectives and define only terms supported by the sources. Cite every "
            "factual statement. Clearly identify any objective the documents do not support.",
        ),
        (
            "Procedure and decision points",
            f"For employee training about {topic}, use only the sources to describe the documented prerequisites, "
            "steps, decision points, "
            "safety checks, exceptions, and escalation path. Keep the steps in source order when the documents show "
            "one. Cite each step and flag missing procedure details instead of inferring them.",
        ),
        (
            "Practice questions",
            f"Create up to five discussion questions for employee training about {topic}. Do not write answers or an "
            "answer key. Use only documented facts, cite each question, and identify when the sources do not support "
            "a question. A separate scored quiz is shown only after an employee submits their answers.",
        ),
    ]


def select_adaptive_question_indexes(
    answer_key: Sequence[dict[str, object]],
    attempts: Sequence[tuple[Sequence[int], Sequence[int]]],
    *,
    limit: int = 5,
) -> list[int]:
    """Prioritize persisted misses and under-practiced items; never generate new source content.

    `attempts` is newest first. Each pair contains question indexes and selected choice indexes,
    allowing full quizzes and prior adaptive subsets to share the same deterministic selector.
    """
    question_count = len(answer_key)
    if not 0 < limit <= 5 or question_count == 0:
        return []

    outcomes: list[list[bool]] = [[] for _ in answer_key]
    for question_indexes, answers in attempts[:10]:
        if len(question_indexes) != len(answers):
            continue
        for question_index, selected in zip(question_indexes, answers, strict=True):
            if not isinstance(question_index, int) or isinstance(question_index, bool):
                continue
            if not 0 <= question_index < question_count:
                continue
            key = answer_key[question_index]
            correct_index = key.get("correct_index") if isinstance(key, dict) else None
            if not isinstance(correct_index, int) or isinstance(correct_index, bool):
                continue
            if not 0 <= selected <= 4:
                continue
            outcomes[question_index].append(selected == correct_index)

    def priority(question_index: int) -> tuple[int, float, int, int]:
        observed = outcomes[question_index]
        most_recent_was_wrong = bool(observed) and not observed[0]
        failure_rate = (len(observed) - sum(observed)) / len(observed) if observed else 0.0
        # After weak recent performance, bring unseen items forward before repeatedly correct ones.
        return (int(most_recent_was_wrong), failure_rate, -len(observed), -question_index)

    return sorted(range(question_count), key=priority, reverse=True)[: min(limit, question_count)]
