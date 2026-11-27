"""Mock mode may quote exact matching evidence, but must not manufacture facts."""

import asyncio
import logging
import uuid

from app.rag.answer import Turn, answer_question
from app.rag.retrieval import RetrievedChunk
from app.schemas.chat import ChatResponse
from tests.fake_llm import MockLLMClient


def _video_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        document_id=uuid.uuid4(),
        filename="synthetic-pump-video.mp4",
        page_number=1,
        chunk_index=0,
        content="On-screen text at 00:00:00.000: QA PUMP PRESSURE 4821",
        score=0.9,
        locator={
            "kind": "video_frame",
            "position": 1,
            "timestamp_seconds": 0.0,
            "label": "Frame at 00:00:00.000",
        },
    )


def test_mock_rag_quotes_matching_source_and_keeps_timestamp_citation():
    chunk = _video_chunk()
    answer = asyncio.run(
        answer_question(
            MockLLMClient(),
            "What pressure value is shown on the synthetic QA pump frame?",
            [chunk],
            general_answers=False,
        )
    )

    assert answer.insufficient_evidence is False
    assert "4821" in answer.answer
    assert answer.provider == "mock"
    assert [(number, cited.locator["label"]) for number, cited in answer.cited] == [
        (1, "Frame at 00:00:00.000")
    ]


def test_mock_rag_refuses_when_source_does_not_match_question():
    chunk = _video_chunk()
    answer = asyncio.run(
        answer_question(
            MockLLMClient(),
            "What is the safety valve maintenance interval?",
            [chunk],
            general_answers=False,
        )
    )

    assert answer.insufficient_evidence is True
    assert answer.cited == []
    assert "4821" not in answer.answer


def test_mock_rag_splits_hyphenated_filename_terms_for_overview():
    chunk = RetrievedChunk(
        chunk_id=2,
        document_id=uuid.uuid4(),
        filename="qa-finance.pdf",
        page_number=1,
        chunk_index=0,
        content=(
            "Synthetic QA Finance procedure. Close the ledger on day 30, reconcile invoice batch F-42, "
            "and route discrepancies to the Finance Controller."
        ),
        score=0.9,
    )
    answer = asyncio.run(
        answer_question(
            MockLLMClient(),
            "Summarize qa-finance.pdf.",
            [chunk],
            whole_document=True,
            general_answers=False,
        )
    )

    assert answer.insufficient_evidence is False
    assert "Synthetic QA Finance procedure." in answer.answer
    assert [(number, cited.filename) for number, cited in answer.cited] == [(1, "qa-finance.pdf")]


def test_mock_rag_followup_prefers_new_fact_over_repeated_context():
    document_id = uuid.uuid4()
    chunk = RetrievedChunk(
        chunk_id=5,
        document_id=document_id,
        filename="synthetic-equipment-policy.pdf",
        page_number=1,
        chunk_index=0,
        content=(
            "Employees may borrow a company headset for up to 12 calendar days. "
            "An extension requires manager approval before the original return date."
        ),
        score=0.9,
    )
    history = [
        Turn(
            question="According to the synthetic equipment policy, how long may an employee borrow a company headset?",
            answer="Employees may borrow a company headset for up to 12 calendar days. [S1]",
            grounded=True,
            documents=(document_id,),
        ),
        Turn(
            question="And what does an extension to that same 12-day borrowing period require?",
            answer="(No answer was given: the documents did not support one.)",
            documents=(document_id,),
        ),
    ]

    answer = asyncio.run(
        answer_question(
            MockLLMClient(),
            "And what does an extension to that same 12-day borrowing period require?",
            [chunk],
            history,
            general_answers=False,
            document_hint=True,
        )
    )

    assert answer.insufficient_evidence is False
    assert "An extension requires manager approval before the original return date." in answer.answer
    assert [(number, cited.filename) for number, cited in answer.cited] == [
        (1, "synthetic-equipment-policy.pdf")
    ]


def test_answer_diagnostics_do_not_log_question_source_or_model_reply(caplog):
    class SyntheticReplyClient:
        async def chat(self, _message: str, *, system_prompt: str | None = None) -> ChatResponse:
            return ChatResponse(
                reply="The requested value is ANSWER_SECRET_SENTINEL [S1]",
                model="synthetic-model",
                provider="synthetic",
            )

    chunk = RetrievedChunk(
        chunk_id=4,
        document_id=uuid.uuid4(),
        filename="synthetic-sensitive-source.pdf",
        page_number=1,
        chunk_index=0,
        content="Synthetic policy value SOURCE_SECRET_SENTINEL is confidential.",
        score=0.9,
    )
    question = "QUESTION_SECRET_SENTINEL: what is the synthetic policy value?"
    caplog.set_level(logging.INFO, logger="app.rag.answer")

    asyncio.run(answer_question(SyntheticReplyClient(), question, [chunk], general_answers=False))

    assert "ANSWER_SECRET_SENTINEL" not in caplog.text
    assert "QUESTION_SECRET_SENTINEL" not in caplog.text
    assert "SOURCE_SECRET_SENTINEL" not in caplog.text
    assert "LLM answer parsed" in caplog.text
