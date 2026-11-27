"""Service-independent validation for the guided knowledge interview flow."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.errors import InvalidInputError
from app.schemas.knowledge_interviews import InterviewTemplateCreate, InterviewTemplateUpdate
from app.services.knowledge_interviews import KnowledgeInterviewService


def test_template_input_rejects_duplicate_keys_and_unsafe_slug():
    base = {
        "slug": "process-capture",
        "title": "Process capture",
        "description": "Capture a reusable process.",
        "questions": [
            {"key": "purpose", "prompt": "What is the purpose?", "required": True},
            {"key": "purpose", "prompt": "What are the steps?", "required": True},
        ],
    }
    with pytest.raises(ValidationError, match="unique"):
        InterviewTemplateCreate.model_validate(base)
    base["slug"] = "../admin"
    base["questions"] = [{"key": "purpose", "prompt": "What is the purpose?", "required": True}]
    with pytest.raises(ValidationError, match="lowercase letters"):
        InterviewTemplateCreate.model_validate(base)


def test_template_update_cannot_be_empty():
    with pytest.raises(ValidationError, match="Change at least one"):
        InterviewTemplateUpdate.model_validate({})


def test_normalize_answers_enforces_saved_template_keys_and_aggregate_limit():
    service = object.__new__(KnowledgeInterviewService)
    session = SimpleNamespace(template_snapshot={"questions": [
        {"key": "purpose"}, {"key": "steps"}, {"key": "third"}, {"key": "fourth"}
    ]})
    normalized = service._normalize_answers(session, {"purpose": "  Hand over the task.  "})
    assert normalized == {"purpose": "Hand over the task."}
    with pytest.raises(InvalidInputError, match="not in this saved template"):
        service._normalize_answers(session, {"other": "no"})
    with pytest.raises(InvalidInputError, match="30,000-character"):
        service._normalize_answers(session, {"purpose": "a" * 10_000, "steps": "b" * 10_000,
                                              "third": "c" * 10_000, "fourth": "d"})


def test_sop_renderer_formats_employee_text_without_claiming_ai_or_citations():
    session = SimpleNamespace(
        sop_title="Monthly close",
        template_snapshot={"questions": [
            {"key": "purpose", "prompt": "What is the purpose?", "required": True},
            {"key": "steps", "prompt": "What are the steps?", "required": True},
            {"key": "optional", "prompt": "Any exceptions?", "required": False},
        ]},
        answers={"purpose": "Close the month safely.", "steps": "Check totals, then reconcile.", "optional": ""},
    )
    body = KnowledgeInterviewService._render_sop(session)
    assert "# Monthly close" in body
    assert "Close the month safely." in body and "Check totals, then reconcile." in body
    assert "Any exceptions?" not in body
    assert "No AI-generated claims or citations have been added." in body
    assert "[S1]" not in body
