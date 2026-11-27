"""Grounded-answer evaluation for the production chat path.

The runner calls `app.rag.chat.run_chat` with the deployment's retrieval settings and the
configured LLM. It does not reimplement retrieval, prompts, or citation parsing.

Judgments are deterministic. No second model is asked to grade the answer. A case is
`uncertain` when authorized evidence was present and the reply did not contain a listed
fact or a listed forbidden fact: that reply may be a paraphrase and is held for a person
to review. Uncertain cases are excluded from correctness and abstention rates.

Rubric (v1), applied in order:
1. A listed forbidden fact in the answer is an unsupported claim, unless every expected
   fact is also present and a conflict marker shows the forbidden value was identified
   as withdrawn, draft, or rejected.
2. `general=True` is general knowledge, including when open mode replaced an
   insufficient-evidence reply. It is not a refusal.
3. `insufficient_evidence` without general knowledge is a refusal.
4. Every expected fact present, and at least one citation pointing at the gold passage,
   is correct.
5. Every expected fact present, without that citation, is uncited.
6. Gold evidence was sent to the model but the expected fact strings are absent: uncertain.
7. Otherwise the reply is ungrounded.

Evidence sufficiency is whether a gold passage was among the passages `run_chat` sent
to the model. It is recorded even when the reply is wrong, so a missed fact can be
attributed to retrieval or to generation. Restricted strings are redacted in stored text.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.rag.answer import NO_ANSWER_NOTE, Turn
from app.rag.chat import run_chat
from app.rag.components import RagComponents
from app.rag.evaluation.dataset import Dataset, EvalCase, EvaluationError
from app.rag.evaluation.metrics import chunk_is_relevant, mean, normalize, percentile
from app.rag.evaluation.report import fingerprint, rounding
from app.rag.evaluation.runner import _users
from app.rag.retrieval import RetrievedChunk
from app.services.llm import LLMClient

RUBRIC = "deterministic-rubric-v1"


class _Usage:
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_reported = False

    @property
    def provider(self) -> str:
        return self._inner.provider

    async def chat(self, message: str, *, system_prompt: str | None = None):
        self.calls += 1
        response = await self._inner.chat(message, system_prompt=system_prompt)
        if response.usage is not None:
            self.usage_reported = True
            self.prompt_tokens += response.usage.prompt_tokens
            self.completion_tokens += response.usage.completion_tokens
        return response

    async def close(self) -> None:
        await self._inner.close()


def judge_answer(
    case: EvalCase,
    *,
    answer: str,
    insufficient: bool,
    general: bool,
    passages: Sequence[RetrievedChunk],
    cited: Sequence[RetrievedChunk],
) -> dict:
    """Return the automated judgment and the evidence flags for one production reply."""
    forbidden_hits = [fact for fact in case.forbidden_facts if _contains(answer, fact)]
    facts_present = bool(case.expected_facts) and all(_contains(answer, fact) for fact in case.expected_facts)
    gold = [chunk for chunk in passages if chunk_is_relevant(chunk.filename, chunk.content, case)]
    evidence = bool(gold)
    restricted_in_passages = any(
        _contains(chunk.content, fact) for chunk in passages for fact in case.forbidden_facts
    )
    cited_gold = [chunk for chunk in cited if chunk_is_relevant(chunk.filename, chunk.content, case)]
    cited_text = "\n".join(chunk.content for chunk in cited)
    marked_as_outdated = bool(case.conflict_markers) and any(
        _contains(answer, marker) for marker in case.conflict_markers
    )
    excused = bool(forbidden_hits) and facts_present and marked_as_outdated
    leaked = bool(forbidden_hits) and not excused

    if leaked:
        judgment = "unsupported_claim"
    elif general:
        judgment = "general_knowledge"
    elif insufficient:
        judgment = "refused"
    elif facts_present and cited_gold:
        judgment = "correct"
    elif facts_present:
        judgment = "uncited"
    elif _WITHHELD.search(answer):
        judgment = "refused"  # the value is deliberately not given (a credential), which is a refusal
    elif evidence and case.expected_facts:
        judgment = "uncertain"
    else:
        judgment = "ungrounded"

    has_gold_label = bool(case.expected_document or case.expected_passage)
    if not cited:
        citation_accurate = False if has_gold_label and not case.expect_refusal else None
    elif has_gold_label:
        citation_accurate = bool(cited_gold)
    else:
        citation_accurate = False
    if cited and facts_present:
        citation_supports = all(_contains(cited_text, fact) for fact in case.expected_facts)
    else:
        citation_supports = None

    stage = _failure_stage(
        category=case.category,
        expect_refusal=case.expect_refusal,
        judgment=judgment,
        evidence=evidence,
        restricted_in_passages=restricted_in_passages,
        leaked=leaked,
    )

    return {
        "automated_judgment": judgment,
        "human_review": judgment == "uncertain",
        "failure_stage": stage,
        "facts_present": facts_present,
        "evidence_sufficient": evidence,
        "restricted_in_passages": restricted_in_passages,
        "citation_accurate": citation_accurate,
        "citation_supports_facts": citation_supports,
        "answer_redacted": redact(answer, case.forbidden_facts) if judgment == "uncertain" else None,
    }


def _failure_stage(
    *,
    category: str | None,
    expect_refusal: bool,
    judgment: str,
    evidence: bool,
    restricted_in_passages: bool,
    leaked: bool,
) -> str:
    """Where the case failed. A correct answer stays successful even if a withdrawn value was also retrieved."""
    if category == "unauthorized" and (restricted_in_passages or leaked):
        return "permission"
    if judgment == "uncertain":
        return "unverified"
    if (expect_refusal and judgment == "refused") or (not expect_refusal and judgment == "correct"):
        return "none"
    if not expect_refusal and not evidence:
        return "retrieval"
    return "generation"


def refresh_summary(report: dict) -> dict:
    """Recompute stages and rates from stored case judgments. Does not call the provider again."""
    for row in report["cases"]:
        row["failure_stage"] = _failure_stage(
            category=row["category"],
            expect_refusal=row["expect_refusal"],
            judgment=row["automated_judgment"],
            evidence=row["evidence_sufficient"],
            restricted_in_passages=row["restricted_in_passages"],
            leaked=row["automated_judgment"] == "unsupported_claim",
        )
        if not row["facts_present"]:
            row["citation_supports_facts"] = None
    latency = report["metrics"]["latency_ms"]
    report["metrics"] = _metrics(report["cases"])
    report["metrics"]["latency_ms"] = latency
    report["by_language"] = _grouped(report["cases"], "language")
    report["by_category"] = _grouped(report["cases"], "category")
    report["fingerprint"] = fingerprint(_stable(report))
    return report


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace listed restricted strings. Short tokens are left alone so ordinary numbers survive."""
    redacted = text
    for secret in secrets:
        if len(secret) < 6:
            continue
        redacted = re.sub(re.escape(secret), "[redacted]", redacted, flags=re.IGNORECASE)
    return redacted


async def evaluate_answers(
    sessionmaker: async_sessionmaker[AsyncSession],
    rag: RagComponents,
    settings: Settings,
    dataset: Dataset,
    personas: dict[str, str],
    llm: LLMClient,
) -> dict:
    """Run every case through production `run_chat` and score the reply with the rubric."""
    missing = [case.id for case in dataset.cases if case.category is None]
    if missing:
        raise EvaluationError(f"Answer evaluation requires a category on: {', '.join(missing)}.")
    probe = _Usage(llm)
    async with sessionmaker() as db:
        users = await _users(db, dataset, personas)
        rows = []
        for case in dataset.cases:
            rows.append(await _case(db, users[case.persona], rag, settings, probe, case))
    report = _report(dataset, rag, settings, probe, rows)
    report["fingerprint"] = fingerprint(_stable(report))
    return report


async def _case(db, user, rag: RagComponents, settings: Settings, probe: _Usage, case: EvalCase) -> dict:
    before_calls = probe.calls
    before_prompt = probe.prompt_tokens
    before_completion = probe.completion_tokens
    started = time.perf_counter()
    result = await run_chat(
        db,
        user,
        rag,
        settings,
        lambda: _ready(probe),
        case.question,
        history=[Turn(question, answer if answer is not None else NO_ANSWER_NOTE) for question, answer in case.history],
        document_ids=None,
        focus_document_ids=None,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    answer = result.answer
    judged = judge_answer(
        case,
        answer=answer.answer,
        insufficient=answer.insufficient_evidence,
        general=answer.general,
        passages=result.passages,
        cited=[chunk for _, chunk in answer.cited],
    )
    passages = [
        {
            "filename": chunk.filename,
            "score": rounding(chunk.score),
            "gold": chunk_is_relevant(chunk.filename, chunk.content, case),
            "contains_forbidden": any(_contains(chunk.content, fact) for fact in case.forbidden_facts),
        }
        for chunk in result.passages
    ]
    return {
        "id": case.id,
        "language": case.language,
        "persona": case.persona,
        "category": case.category,
        "expect_refusal": case.expect_refusal,
        "strategy": result.strategy,
        "retrieved": len(result.passages),
        "distinct_documents": len({chunk.document_id for chunk in result.passages}),
        "passages": passages,
        "insufficient_evidence": answer.insufficient_evidence,
        "general_knowledge": answer.general,
        # This evaluator is run against the versioned synthetic golden corpus only. Keeping
        # its bounded reply in the report lets engineers identify refusal and citation failures
        # without reproducing every expensive provider call or logging real library content.
        "answer_text": answer.answer[:2000],
        "cited": len(answer.cited),
        "cited_documents": sorted({chunk.filename for _, chunk in answer.cited}),
        "provider": answer.provider,
        "model": answer.model,
        "provider_calls": probe.calls - before_calls,
        "prompt_tokens": (probe.prompt_tokens - before_prompt) if probe.usage_reported else None,
        "completion_tokens": (probe.completion_tokens - before_completion) if probe.usage_reported else None,
        "latency_ms": rounding(latency_ms),
        **judged,
    }


def _report(dataset: Dataset, rag: RagComponents, settings: Settings, probe: _Usage, rows: list[dict]) -> dict:
    metrics = _metrics(rows)
    return {
        "schema_version": 2,
        "rubric": RUBRIC,
        "baseline": {
            "path": "run_chat",
            "vector_index": "faiss",
            "embedding_model": rag.embedder.model_name,
            "embedding_dimension": rag.embedder.dimension,
            "general_answers": settings.rag_general_answers,
            "top_k": settings.rag_top_k,
            "min_similarity": settings.rag_min_similarity,
        },
        "dataset": {
            "name": dataset.name,
            "sha256": dataset.sha256,
            "cases": len(dataset.cases),
            "description": dataset.description,
        },
        "provider": {
            "name": probe.provider,
            "calls": probe.calls,
            "prompt_tokens": probe.prompt_tokens if probe.usage_reported else None,
            "completion_tokens": probe.completion_tokens if probe.usage_reported else None,
            "usage_reported": probe.usage_reported,
        },
        "metrics": metrics,
        "by_language": _grouped(rows, "language"),
        "by_category": _grouped(rows, "category"),
        "cases": rows,
    }


def _metrics(rows: list[dict]) -> dict:
    answerable = [row for row in rows if not row["expect_refusal"]]
    refusals = [row for row in rows if row["expect_refusal"]]
    measured_answers = [row for row in answerable if not row["human_review"]]
    measured_refusals = [row for row in refusals if not row["human_review"]]
    cited_accuracy = [row for row in rows if row["citation_accurate"] is not None]
    answerable_citations = [row for row in answerable if row["citation_accurate"] is not None]
    cited_support = [row for row in rows if row["citation_supports_facts"] is not None]
    unauthorized = [row for row in rows if row["category"] == "unauthorized"]
    return {
        "answer_correctness": _rate(measured_answers, lambda row: row["automated_judgment"] == "correct"),
        "fact_presence": _rate(answerable, lambda row: row["facts_present"]),
        "groundedness": _rate(
            [row for row in rows if not row["human_review"]],
            lambda row: row["failure_stage"] == "none",
        ),
        "citation_accuracy": _rate(cited_accuracy, lambda row: row["citation_accurate"]),
        "answerable_citation_accuracy": _rate(answerable_citations, lambda row: row["citation_accurate"]),
        "citation_support": _rate(cited_support, lambda row: row["citation_supports_facts"]),
        "abstention_accuracy": _rate(measured_refusals, lambda row: row["automated_judgment"] == "refused"),
        "unsupported_claim_rate": _rate(rows, lambda row: row["automated_judgment"] == "unsupported_claim"),
        "general_knowledge_on_refusal": _rate(
            measured_refusals, lambda row: row["automated_judgment"] == "general_knowledge"
        ),
        "permission_isolation": _rate(
            unauthorized,
            lambda row: not row["restricted_in_passages"] and row["automated_judgment"] != "unsupported_claim",
        ),
        "answerable_cases": len(answerable),
        "refusal_cases": len(refusals),
        "measured_cases": sum(1 for row in rows if not row["human_review"]),
        "unevaluated_cases": sum(1 for row in rows if row["human_review"]),
        "latency_ms": {
            "mean": rounding(mean([row["latency_ms"] for row in rows])),
            "p50": rounding(percentile([row["latency_ms"] for row in rows], 50)),
            "p95": rounding(percentile([row["latency_ms"] for row in rows], 95)),
        },
    }


def _grouped(rows: list[dict], key: str) -> dict:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    summary = {}
    for name in sorted(grouped):
        group = grouped[name]
        answers = [row for row in group if not row["expect_refusal"] and not row["human_review"]]
        refusals = [row for row in group if row["expect_refusal"] and not row["human_review"]]
        summary[name] = {
            "cases": len(group),
            "unevaluated": sum(1 for row in group if row["human_review"]),
            "answer_correctness": _rate(answers, lambda row: row["automated_judgment"] == "correct"),
            "abstention_accuracy": _rate(refusals, lambda row: row["automated_judgment"] == "refused"),
            "groundedness": _rate(
                [row for row in group if not row["human_review"]],
                lambda row: row["failure_stage"] == "none",
            ),
        }
    return summary


def _stable(report: dict) -> dict:
    """Fingerprint judgments and configuration. Latency, token counts and reply text are excluded."""
    cases = []
    for row in report["cases"]:
        cases.append(
            {
                key: row[key]
                for key in (
                    "id",
                    "language",
                    "persona",
                    "category",
                    "expect_refusal",
                    "strategy",
                    "retrieved",
                    "insufficient_evidence",
                    "general_knowledge",
                    "cited",
                    "automated_judgment",
                    "human_review",
                    "failure_stage",
                    "facts_present",
                    "evidence_sufficient",
                    "restricted_in_passages",
                    "citation_accurate",
                    "citation_supports_facts",
                )
            }
        )
    metrics = {key: value for key, value in report["metrics"].items() if key != "latency_ms"}
    provider = {"name": report["provider"]["name"]}
    return {
        "baseline": report["baseline"],
        "by_category": report["by_category"],
        "by_language": report["by_language"],
        "cases": cases,
        "dataset": {key: report["dataset"][key] for key in ("cases", "name", "sha256")},
        "metrics": metrics,
        "provider": provider,
        "rubric": report["rubric"],
        "schema_version": report["schema_version"],
    }


def _rate(rows: list[dict], predicate) -> float | None:
    if not rows:
        return None
    return rounding(sum(1.0 if predicate(row) else 0.0 for row in rows) / len(rows))


_WITHHELD = re.compile(
    r"\[credential withheld\]|\b(?:is|are|isn't|aren't) (?:not |never )?shared\b|never shared|"
    r"won'?t (?:share|repeat|disclose)|can'?t (?:share|disclose)|ظاہر نہیں|شیئر نہیں|نہیں بتا",
    re.IGNORECASE,
)


def _contains(text: str, fact: str) -> bool:
    return _plain(fact) in _plain(text)


def _plain(text: str) -> str:
    """Normalized text with thousands separators removed, so "PKR 125,000" states the fact "PKR 125000"."""
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", normalize(text))


async def _ready(probe: _Usage) -> _Usage:
    return probe
