"""Step AB slice 4 — Roman Urdu answers: language mirroring in the grounded-answer path.

Pure unit tests (no database, no network). The answer language mirrors the
question: a Roman Urdu question gets a Roman Urdu answer, an Urdu-script
question gets an Urdu-script answer, English stays English. Mirroring is
presentation only — the facts, the citations and the evidence gate never change.
"""

import asyncio
import uuid
from types import SimpleNamespace

from app.rag.answer import (
    INSUFFICIENT_MARKER,
    NO_SOURCES_ANSWER,
    UNGROUNDED_ANSWER,
    UNVERIFIED_FACT,
    answer_language_instruction,
    answer_question,
    general_answer,
    localized_no_sources,
    localized_ungrounded,
    localized_unverified,
    parse_answer,
    small_talk_reply,
)
from app.rag.auto_enrich import answer_language
from app.rag.retrieval import RetrievedChunk

RU_QUESTION = "pak a ka next match kb hai?"
RU_DOC_QUESTION = "is contract mein payment terms kya hain?"
UR_QUESTION = "اس معاہدے میں ادائیگی کی شرائط کیا ہیں؟"
EN_QUESTION = "What are the payment terms in this contract?"


class FakeLLM:
    """Captures the prompt; answers with a canned reply."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.prompts: list[str | None] = []
        self.messages: list[str] = []

    async def chat(self, message, *, system_prompt=None):
        self.messages.append(message)
        self.prompts.append(system_prompt)
        return SimpleNamespace(reply=self.reply, model="test-model", provider="test-provider")


def _chunks(*texts: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(i, uuid.uuid4(), f"doc{i}.pdf", page_number=i + 1, chunk_index=0, content=t, score=0.9)
        for i, t in enumerate(texts)
    ]


# ------------------------------------------------------------- language detection


def test_answer_language_mirrors_the_question():
    assert answer_language(RU_QUESTION) == "roman_urdu"
    assert answer_language(RU_DOC_QUESTION) == "roman_urdu"
    assert answer_language(UR_QUESTION) == "urdu"
    assert answer_language(EN_QUESTION) == "english"


def test_answer_language_instruction_is_empty_for_english():
    assert answer_language_instruction(EN_QUESTION) == ""


def test_answer_language_instruction_names_roman_urdu():
    instruction = answer_language_instruction(RU_QUESTION)
    assert "Roman Urdu" in instruction
    assert "only the wording changes" in instruction


# ------------------------------------------------------------- small talk (canned replies)


def test_small_talk_roman_urdu_identity():
    reply = small_talk_reply("tum kya kar sakte ho?")
    assert reply is not None
    assert "InfraSpine AI" in reply
    assert "jawab" in reply
    assert "your organization's knowledge assistant" not in reply


def test_small_talk_bare_shukriya_stays_english():
    # "shukriya" alone carries no Roman Urdu marker word, so the slice-1
    # detector reports English and the English thanks is returned.
    assert small_talk_reply("shukriya") == "You're welcome! Ask another question about your documents any time."


def test_urdu_script_instruction_names_urdu_script():
    instruction = answer_language_instruction(UR_QUESTION)
    assert "Urdu script" in instruction


def test_small_talk_english_unchanged():
    assert small_talk_reply("who are you?") is not None
    assert "your organization's knowledge assistant" in small_talk_reply("who are you?")
    assert small_talk_reply("thanks") == "You're welcome! Ask another question about your documents any time."


def test_small_talk_not_triggered_by_a_real_question():
    assert small_talk_reply(RU_DOC_QUESTION) is None
    assert small_talk_reply(EN_QUESTION) is None


# ------------------------------------------------------------- localized refusals


def test_localized_refusals_mirror_the_question():
    assert "maloomat nahi mili" in localized_no_sources(RU_QUESTION)
    assert "دستاویزات" in localized_no_sources(UR_QUESTION)
    assert localized_no_sources(EN_QUESTION) == NO_SOURCES_ANSWER
    assert "sabit ho" in localized_ungrounded(RU_DOC_QUESTION)
    assert localized_ungrounded(EN_QUESTION) == UNGROUNDED_ANSWER
    assert "tasdeeq" in localized_unverified(RU_DOC_QUESTION)
    assert localized_unverified(EN_QUESTION) == UNVERIFIED_FACT


def test_parse_answer_language_param():
    chunks = _chunks("a", "b")
    # A marker WITH an explanation returns the model's own sentence (the
    # model, instructed in Roman Urdu, writes it in Roman Urdu itself).
    answer, insufficient, _ = parse_answer(f"{INSUFFICIENT_MARKER}: the sources do not mention it.", chunks, "roman_urdu")
    assert insufficient and answer == "the sources do not mention it."
    # A bare marker with no explanation falls back to the localized refusal.
    answer, insufficient, _ = parse_answer(f"{INSUFFICIENT_MARKER}:", chunks, "roman_urdu")
    assert insufficient and "maloomat nahi mili" in answer
    answer, insufficient, _ = parse_answer(f"{INSUFFICIENT_MARKER}:", chunks)
    assert insufficient and answer == NO_SOURCES_ANSWER
    answer, insufficient, _ = parse_answer("A confident answer with no citations.", chunks, "roman_urdu")
    assert insufficient and "sabit ho" in answer


# ------------------------------------------------------------- answer_question wiring


def test_no_provider_roman_urdu_question():
    result = asyncio.run(answer_question(None, RU_QUESTION, []))
    assert result.insufficient_evidence
    assert "maloomat nahi mili" in result.answer
    assert result.cited == []


def test_no_chunks_roman_urdu_question_never_calls_the_llm():
    llm = FakeLLM(reply="The match is on Friday [S1].")
    result = asyncio.run(answer_question(llm, RU_QUESTION, []))
    assert result.insufficient_evidence
    assert "maloomat nahi mili" in result.answer
    assert llm.messages == []  # no LLM call was made


def test_no_chunks_english_question_unchanged():
    llm = FakeLLM()
    result = asyncio.run(answer_question(llm, "When is the next match?", []))
    assert result.insufficient_evidence
    assert result.answer == NO_SOURCES_ANSWER


def test_no_chunks_urdu_script_question():
    result = asyncio.run(answer_question(FakeLLM(), UR_QUESTION, []))
    assert result.insufficient_evidence
    assert "دستاویزات" in result.answer


def test_insufficient_marker_roman_urdu_question():
    chunks = _chunks("The contract is valid until 31 December 2026.")
    # Marker with an explanation: the model's own sentence is shown as-is.
    llm = FakeLLM(reply="INSUFFICIENT_EVIDENCE: the sources do not mention the match.")
    result = asyncio.run(answer_question(llm, RU_QUESTION, chunks))
    assert result.insufficient_evidence
    assert result.answer == "the sources do not mention the match."
    # Bare marker: the localized refusal is shown instead.
    llm = FakeLLM(reply="INSUFFICIENT_EVIDENCE:")
    result = asyncio.run(answer_question(llm, RU_QUESTION, chunks))
    assert result.insufficient_evidence
    assert "maloomat nahi mili" in result.answer


def test_uncited_reply_roman_urdu_question():
    chunks = _chunks("The contract is valid until 31 December 2026.")
    llm = FakeLLM(reply="The contract ends next year, trust me.")
    result = asyncio.run(answer_question(llm, RU_DOC_QUESTION, chunks))
    assert result.insufficient_evidence
    assert "sabit ho" in result.answer


def test_prompt_carries_the_roman_urdu_instruction():
    chunks = _chunks("The contract ends on 31 December 2026.")
    llm = FakeLLM(reply="The contract ends on 31 December 2026 [S1].")
    result = asyncio.run(answer_question(llm, "contract kb khatam ho raha hai?", chunks))
    assert llm.prompts and "Answer in Roman Urdu" in (llm.prompts[0] or "")
    # Same facts, same citations — mirroring changes wording only.
    assert not result.insufficient_evidence
    assert "31 December 2026" in result.answer
    assert "[S1]" in result.answer
    assert [n for n, _ in result.cited] == [1]


def test_prompt_has_no_language_instruction_for_english():
    chunks = _chunks("The contract ends on 31 December 2026.")
    llm = FakeLLM(reply="The contract ends on 31 December 2026 [S1].")
    asyncio.run(answer_question(llm, "When does the contract end?", chunks))
    assert llm.prompts and "Roman Urdu" not in (llm.prompts[0] or "") and "Urdu script" not in (llm.prompts[0] or "")


def test_unverified_roman_urdu_question():
    chunks = _chunks("The contract is valid until 31 December 2026.")
    llm = FakeLLM(reply="The payment terms are net 30, probably.")
    result = asyncio.run(
        answer_question(llm, RU_DOC_QUESTION, chunks, general_answers=True, document_hint=True)
    )
    assert result.insufficient_evidence
    assert "tasdeeq" in result.answer


def test_general_answer_empty_reply_roman_urdu():
    llm = FakeLLM(reply="")
    result = asyncio.run(general_answer(llm, RU_QUESTION, None, None))
    assert result.insufficient_evidence
    assert "maloomat nahi mili" in result.answer
    assert llm.prompts and "Answer in Roman Urdu" in (llm.prompts[0] or "")
