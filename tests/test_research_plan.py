"""The research planner is bounded and fails back to the user's original query."""

import asyncio

from app.rag.research import research_plan
from app.rag.training import training_sections
from app.schemas.chat import ChatResponse


class PlannerClient:
    provider = "test"

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        self.calls += 1
        assert message
        assert "bounded research coordinator" in (system_prompt or "")
        assert "Do not" in (system_prompt or "") and "choose or name tools" in (system_prompt or "")
        return ChatResponse(reply=self.reply, model="test-model", provider="test")


def test_research_plan_limits_questions_and_deduplicates():
    client = PlannerClient(
        '{"assignments":['
        '{"specialist":"evidence_finder","question":"Cause A?"},'
        '{"specialist":"policy_analyst","question":"Cause B?"},'
        '{"specialist":"exception_checker","question":"Cause A?"}]}'
    )
    result = asyncio.run(research_plan(client, "Compare the causes."))
    assert [(item.specialist, item.question) for item in result] == [
        ("evidence_finder", "Cause A?"),
        ("policy_analyst", "Cause B?"),
    ]
    assert client.calls == 1


def test_research_plan_falls_back_to_original_question_on_invalid_output():
    client = PlannerClient("not json")
    result = asyncio.run(research_plan(client, "What changed in the outage report?"))
    assert [(item.specialist, item.question) for item in result] == [
        ("evidence_finder", "What changed in the outage report?")
    ]


def test_research_plan_rejects_unregistered_specialists_and_tool_requests():
    for reply in (
        '{"assignments":[{"specialist":"browser_controller","question":"Open an old system."}]}',
        '{"assignments":[{"specialist":"evidence_finder","question":"Find evidence.","tool":"browser"}]}',
        '{"assignments":[{"specialist":"evidence_finder","question":"Find evidence."}],"actions":["open"]}',
    ):
        result = asyncio.run(research_plan(PlannerClient(reply), "Find evidence in the reports."))
        assert [(item.specialist, item.question) for item in result] == [
            ("evidence_finder", "Find evidence in the reports.")
        ]


def test_research_plan_rejects_over_budget_or_duplicate_specialists():
    over_budget = '{"assignments":[' + ",".join(
        f'{{"specialist":"{role}","question":"Question {index}?"}}'
        for index, role in enumerate(("evidence_finder", "policy_analyst", "exception_checker", "evidence_finder"))
    ) + ']}'
    duplicate_specialist = (
        '{"assignments":['
        '{"specialist":"evidence_finder","question":"Question one?"},'
        '{"specialist":"evidence_finder","question":"Question two?"}]}'
    )
    for reply in (over_budget, duplicate_specialist):
        result = asyncio.run(research_plan(PlannerClient(reply), "Compare the reports."))
        assert [(item.specialist, item.question) for item in result] == [
            ("evidence_finder", "Compare the reports.")
        ]


def test_training_sections_are_bounded_and_cover_lesson_procedure_and_assessment():
    sections = training_sections("  incident\n handover ")

    assert [title for title, _ in sections] == [
        "Learning objectives and essentials",
        "Procedure and decision points",
        "Practice questions",
    ]
    assert len(sections) == 3
    assert all("incident handover" in prompt for _, prompt in sections)
    assert all("sources" in prompt.lower() for _, prompt in sections)
    assert "do not write answers or an answer key" in sections[-1][1].lower()
    assert "separate scored quiz" in sections[-1][1].lower()

