"""Offline LLM test double (not part of the application)."""

import html
import json
import re

from app.schemas.chat import ChatResponse


class MockLLMClient:
    """Deterministic offline LLM double for tests: answers only with an exact matching source sentence."""

    provider = "mock"

    @staticmethod
    def _source_excerpt(message: str) -> tuple[str, int] | None:
        """Return the single source sentence that best matches the question.

        It must not
        invent a generic, apparently grounded answer: the only factual output
        it can produce is an exact sentence already present in an authorized
        source block. The normal RAG support and citation checks still run.
        """
        question_match = re.search(r"(?:^|\n)QUESTION:\s*\n(.*)\s*$", message, re.DOTALL)
        if question_match is None:
            return None
        stop_words = {
            "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
            "give", "how", "i", "in", "is", "it", "me", "of", "on", "or", "please", "show",
            "same", "tell", "that", "the", "this", "to", "was", "what", "when", "where", "which",
            "who", "why", "with",
        }

        def terms(text: str) -> set[str]:
            normalized: set[str] = set()
            for raw in re.findall(r"[A-Za-z0-9]{2,}|\d{2,}", text):
                term = raw.casefold()
                if term in stop_words:
                    continue
                if len(term) > 5 and term.endswith("ies"):
                    term = term[:-3] + "y"
                elif len(term) > 6 and term.endswith("ing"):
                    term = term[:-3]
                elif len(term) > 5 and term.endswith("ed"):
                    term = term[:-2]
                elif len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
                    term = term[:-1]
                normalized.add(term)
            return normalized

        question_terms = terms(question_match.group(1))
        if not question_terms:
            return None

        # In a follow-up, repeated words often identify the earlier subject while the new words
        # identify the fact being asked for now. Prefer the latest supported turn's novel intent;
        # failed turns deliberately carry NO_ANSWER_NOTE and must not steer this choice.
        background_terms: set[str] = set()
        conversation = re.search(
            r"CONVERSATION \(context only, not a source\):\n(.*?)\n\nSOURCES(?:\(|:)",
            message,
            re.DOTALL,
        )
        if conversation is not None:
            turns = re.findall(
                r"(?:^|\n)User:\s*(.*?)\nAssistant:\s*(.*?)(?=\n\nUser:|\Z)",
                conversation.group(1),
                re.DOTALL,
            )
            supported_turns = [
                (prior_question, prior_answer)
                for prior_question, prior_answer in turns
                if "(No answer was given: the documents did not support one.)" not in prior_answer
            ]
            if supported_turns:
                prior_question, prior_answer = supported_turns[-1]
                background_terms = terms(f"{prior_question} {prior_answer}")
        novel_terms = question_terms - background_terms

        sources = re.finditer(
            r'<source\s+id="S(\d+)"[^>]*>\s*(.*?)\s*</source>', message, re.DOTALL
        )
        best: tuple[int, int, str] | None = None
        for source in sources:
            source_id = int(source.group(1))
            content = html.unescape(source.group(2))
            sentences = [
                sentence.strip()
                for line in content.splitlines()
                for sentence in re.split(r"(?<=[.!?])\s+", line)
                if sentence.strip()
            ]
            for sentence in sentences:
                sentence_terms = terms(sentence)
                overlap = len(question_terms & sentence_terms)
                if not overlap:
                    continue
                if novel_terms:
                    novel_overlap = len(novel_terms & sentence_terms)
                    repeated_overlap = overlap - novel_overlap
                    score = novel_overlap * 3 + repeated_overlap * 0.25
                else:
                    score = overlap
                if score and (best is None or score > best[0]):
                    best = (score, source_id, sentence)
        return (best[2], best[1]) if best is not None else None

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        if system_prompt is not None and "chart data" in system_prompt:
            # Chart request: plot the first few figures written in source S1 (so they pass verification).
            first = re.search(r'<source id="S1"[^>]*>\n(.*?)\n</source>', message, re.DOTALL)
            numbers = list(dict.fromkeys(re.findall(r"(?<![\d.])\d+(?![\d.])", first.group(1) if first else "")))[:5]
            if not numbers:
                return ChatResponse(reply="INSUFFICIENT_EVIDENCE: no figures in the sources.", model="mock-model", provider="mock")
            points = [{"category": f"Figure {i}", "series": "Value", "value": int(n), "source": "S1"} for i, n in enumerate(numbers, 1)]
            reply = json.dumps({"title": "[mock] Figures from the first source", "unit": "", "summary": "Mock chart [S1].", "points": points})
            return ChatResponse(reply=reply, model="mock-model", provider="mock", usage=None)
        if system_prompt is not None:  # e.g. RAG: emit only exact, matching source text
            excerpt = self._source_excerpt(message)
            reply = (
                f"{excerpt[0]} [S{excerpt[1]}]"
                if excerpt
                else "INSUFFICIENT_EVIDENCE: no source sentence matches the question in the test client."
            )
            return ChatResponse(reply=reply, model="mock-model", provider="mock", usage=None)
        preview = message if len(message) <= 80 else message[:77] + "..."
        return ChatResponse(
            reply=f'[mock] InfraSpine AI received: "{preview}".',
            model="mock-model",
            provider="mock",
            usage=None,
        )

    async def verify(self, timeout_seconds: float) -> None:
        return None

    async def close(self) -> None:
        return None
