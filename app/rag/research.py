"""Bounded internal specialist planning; every specialist uses the same ACL-checked RAG path."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.llm import LLMClient

MAX_RESEARCH_QUESTIONS = 3
MAX_PLANNER_RESPONSE_CHARS = 12_000
ResearchSpecialist = Literal["evidence_finder", "policy_analyst", "exception_checker"]

PLANNER_PROMPT = """You are the bounded research coordinator for an internal document research tool.
Delegate the request to at most three distinct read-only specialists, choosing only from:
- evidence_finder: find direct statements and exact figures that answer the request.
- policy_analyst: find applicable internal policies, owners, thresholds, and procedures.
- exception_checker: find stated exceptions, conflicting evidence, and missing evidence.
Each specialist receives one short evidence question. Keep every question within the user's stated topic.
Do not answer, add assumptions, request data, choose or name tools, browse the web, or propose side effects.
The server independently fixes every specialist to the existing read-only knowledge search with the caller's ACL.
Return only JSON in this exact shape, with one to three assignments and no extra keys:
{"assignments":[{"specialist":"evidence_finder","question":"..."}]}"""

SPECIALIST_PROMPTS: dict[ResearchSpecialist, str] = {
    "evidence_finder": (
        "Specialist focus: locate direct, source-stated evidence for this question. Preserve exact values, "
        "dates, owners, and qualifications; do not infer unstated facts."
    ),
    "policy_analyst": (
        "Specialist focus: identify source-stated policy rules, thresholds, owners, and procedures relevant to "
        "this question. Do not provide legal interpretation or infer policy beyond the cited text."
    ),
    "exception_checker": (
        "Specialist focus: look for source-stated exceptions, contradictions, changed versions, and evidence gaps "
        "relevant to this question. Treat a missing result as an evidence gap, not proof that something never occurred."
    ),
}

class ResearchAssignment(BaseModel):
    """Validated planner output; the planner cannot select capabilities or invoke tools."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    specialist: ResearchSpecialist
    question: str = Field(min_length=1, max_length=4000)


def _fallback(question: str) -> list[ResearchAssignment]:
    return [ResearchAssignment(specialist="evidence_finder", question=question.strip())]


async def research_plan(client: LLMClient, question: str) -> list[ResearchAssignment]:
    """Return at most three allowlisted, read-only assignments; malformed plans run one safe query."""
    fallback = _fallback(question)
    response = await client.chat(question, system_prompt=PLANNER_PROMPT)
    raw = response.reply.strip()
    if len(raw) > MAX_PLANNER_RESPONSE_CHARS:
        return fallback
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return fallback
    if not isinstance(parsed, dict) or set(parsed) != {"assignments"}:
        return fallback
    candidates = parsed["assignments"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_RESEARCH_QUESTIONS:
        return fallback

    assignments: list[ResearchAssignment] = []
    used_specialists: set[str] = set()
    used_questions: set[str] = set()
    try:
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {"specialist", "question"}:
                return fallback
            assignment = ResearchAssignment.model_validate(candidate)
            specialist = assignment.specialist
            question_key = assignment.question.casefold()
            if specialist in used_specialists:
                return fallback
            if question_key in used_questions:
                continue
            used_specialists.add(specialist)
            used_questions.add(question_key)
            assignments.append(assignment)
    except (ValidationError, TypeError, ValueError):
        return fallback
    return assignments or fallback
