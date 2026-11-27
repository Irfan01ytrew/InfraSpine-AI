"""Enterprise workload evaluation: realistic document libraries and multi-turn conversations, scored end to end.

The golden gate (`evals/golden.json`) asks single-turn questions with one expected document each. Real use is
different: people name files, months, sites and dates that often appear only in a filename; they ask follow-ups
that lean on, or switch away from, the previous turn; they compare documents; they write Roman Urdu or Urdu about
English documents; and a large repetitive log sits next to a one-page memo. This module adds that workload:

* `corpus()` - synthetic Northwind Traders documents (not customer data), seeded beside the Harborline golden
  corpus: near-identical monthly bills told apart only by their filename, minutes and audits whose date or site
  is only in the filename, a superseded policy, two vendor contracts, a small table memo, a 2,000-row
  repetitive log, Urdu and Roman Urdu notes, an HR-restricted spreadsheet and a private note.
* `load_workloads()` - the case format of `evals/enterprise_workloads.json`. A case may carry conversation
  history with the documents each earlier answer cited, several expected documents, the answer-bearing passages,
  facts that belong only to earlier turns (`history_facts`), and documents a persona must never receive.
* `history_and_focus()` - rebuilds history and focus exactly as `ConversationService.context` does for a saved
  conversation, so follow-ups go through production `run_chat` with the same inputs as in the product.
* `judge_retrieval()` / `judge_answer()` / `summarize()` - deterministic scoring. Retrieval is judged on the
  passages `run_chat` hands to the model; answers reuse the rubric order of `app.rag.evaluation.answers`
  (forbidden fact, general knowledge, refusal, cited facts) and add conversation-level checks.

Nothing here changes retrieval, routing, prompts or evidence checks: it only measures them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from app.rag.evaluation.dataset import LANGUAGES, EvaluationError
from app.rag.evaluation.metrics import mean, normalize, percentile

SCENARIOS = (
    "discovery",  # find a document by its filename, month, date, site or vendor
    "fragmentation",  # the answer sits in one section or table of a small document
    "followup",  # a turn that leans on, or switches away from, the previous turns
    "multilingual",  # Urdu or Roman Urdu questions, documents in another language
    "multidoc",  # the answer needs two or more documents
    "grounding",  # superseded values, near misses, absent facts, general questions
    "acl",  # restricted and private documents, positive and negative
    "repetitive",  # a large repetitive document competes with a short relevant one
)
EXPECTATIONS = ("answer", "refusal", "general")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_PERSONA = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Access beyond "shared": a group-restricted spreadsheet (the member persona is in the group) and a private note.
HR_GROUP = "Northwind HR"
RESTRICTED = {"Salary-Revision-2026-Confidential.xlsx": HR_GROUP}
PRIVATE = {"CEO-Private-Notes.txt": "owner"}


# ------------------------------------------------------------------ cases


@dataclass(frozen=True)
class HistoryTurn:
    question: str
    answer: str | None  # None: the documents did not support an answer (stored as insufficient evidence)
    cited: tuple[str, ...] = ()  # filenames the answer cited; they become the conversation's focus


@dataclass(frozen=True)
class Evidence:
    document: str
    passage: str


@dataclass(frozen=True)
class WorkloadCase:
    id: str
    scenario: str
    language: str
    persona: str
    question: str
    expect: str
    history: tuple[HistoryTurn, ...] = ()
    expected_documents: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    # Each fact must appear in the answer. "a|b" accepts either form (a translation, a unit written differently).
    expected_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    conflict_markers: tuple[str, ...] = ()
    history_facts: tuple[str, ...] = ()  # facts of earlier turns' documents; this answer must not repeat them
    forbidden_documents: tuple[str, ...] = ()  # the persona must never receive passages from these
    distractor_documents: tuple[str, ...] = ()  # documents whose share of the passages is reported
    independent: bool = False  # the question does not lean on the history (a topic switch)
    note: str = ""


@dataclass(frozen=True)
class Workloads:
    name: str
    description: str
    cases: tuple[WorkloadCase, ...]
    sha256: str


_CASE_FIELDS = {
    "id", "scenario", "language", "persona", "question", "expect", "history", "expected_documents", "evidence",
    "expected_facts", "forbidden_facts", "conflict_markers", "history_facts", "forbidden_documents",
    "distractor_documents", "independent", "note",
}


def load_workloads(path: Path) -> Workloads:
    import hashlib

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise EvaluationError(f"File not found: {path.name}.") from None
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"{path.name} is not valid JSON ({exc.lineno}:{exc.colno}).") from None
    if not isinstance(raw, dict) or set(raw) - {"name", "description", "cases"}:
        raise EvaluationError("A workload file is a JSON object with name, description and cases.")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise EvaluationError("A workload file needs at least one case.")
    cases: list[WorkloadCase] = []
    seen: set[str] = set()
    for index, item in enumerate(cases_raw, start=1):
        case = _case(item, index)
        if case.id in seen:
            raise EvaluationError(f"Duplicate case id {case.id!r}.")
        seen.add(case.id)
        cases.append(case)
    return Workloads(
        name=_text(raw.get("name"), "name", 80),
        description=raw.get("description", "") if isinstance(raw.get("description", ""), str) else "",
        cases=tuple(cases),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _case(item: object, index: int) -> WorkloadCase:
    if not isinstance(item, dict):
        raise EvaluationError(f"Case {index} must be a JSON object.")
    unknown = set(item) - _CASE_FIELDS
    if unknown:
        raise EvaluationError(f"Case {index} has unknown fields: {', '.join(sorted(unknown))}.")
    case_id = _text(item.get("id"), f"case {index} id", 80)
    if not _ID.fullmatch(case_id):
        raise EvaluationError(f"Case id {case_id!r} must be lowercase letters, digits and hyphens.")
    scenario, language, persona, expect = (item.get(key) for key in ("scenario", "language", "persona", "expect"))
    if scenario not in SCENARIOS:
        raise EvaluationError(f"Case {case_id} scenario must be one of: {', '.join(SCENARIOS)}.")
    if language not in LANGUAGES:
        raise EvaluationError(f"Case {case_id} language must be one of: {', '.join(LANGUAGES)}.")
    if not isinstance(persona, str) or not _PERSONA.fullmatch(persona):
        raise EvaluationError(f"Case {case_id} persona must be a short lowercase identifier.")
    if expect not in EXPECTATIONS:
        raise EvaluationError(f"Case {case_id} expect must be one of: {', '.join(EXPECTATIONS)}.")
    documents = _strings(item.get("expected_documents"), f"case {case_id} expected_documents")
    facts = _strings(item.get("expected_facts"), f"case {case_id} expected_facts")
    evidence = tuple(_evidence(entry, case_id) for entry in item.get("evidence") or [])
    if expect == "answer" and (not documents or not facts):
        raise EvaluationError(f"Case {case_id} expects an answer: it needs expected_documents and expected_facts.")
    if expect != "answer" and documents:
        raise EvaluationError(f"Case {case_id} expects no document answer, so it cannot list expected_documents.")
    stray = [entry.document for entry in evidence if entry.document not in documents]
    if stray:
        raise EvaluationError(f"Case {case_id} evidence names documents it does not expect: {', '.join(stray)}.")
    independent = item.get("independent", False)
    if not isinstance(independent, bool):
        raise EvaluationError(f"Case {case_id} independent must be true or false.")
    history = tuple(_turn(turn, case_id) for turn in item.get("history") or [])
    if independent and not history:
        raise EvaluationError(f"Case {case_id} is independent of a history it does not have.")
    return WorkloadCase(
        id=case_id,
        scenario=scenario,
        language=language,
        persona=persona,
        question=_text(item.get("question"), f"case {case_id} question", 2000),
        expect=expect,
        history=history,
        expected_documents=documents,
        evidence=evidence,
        expected_facts=facts,
        forbidden_facts=_strings(item.get("forbidden_facts"), f"case {case_id} forbidden_facts"),
        conflict_markers=_strings(item.get("conflict_markers"), f"case {case_id} conflict_markers"),
        history_facts=_strings(item.get("history_facts"), f"case {case_id} history_facts"),
        forbidden_documents=_strings(item.get("forbidden_documents"), f"case {case_id} forbidden_documents"),
        distractor_documents=_strings(item.get("distractor_documents"), f"case {case_id} distractor_documents"),
        independent=independent,
        note=item.get("note", "") if isinstance(item.get("note", ""), str) else "",
    )


def _turn(value: object, case_id: str) -> HistoryTurn:
    if not isinstance(value, dict) or set(value) - {"question", "answer", "cited"} or "question" not in value:
        raise EvaluationError(f"Case {case_id} history turns need a question, an optional answer and cited files.")
    answer = value.get("answer")
    if answer is not None:
        answer = _text(answer, f"case {case_id} history answer", 4000)
    cited = _strings(value.get("cited"), f"case {case_id} history cited")
    if answer is None and cited:
        raise EvaluationError(f"Case {case_id}: an unanswered history turn cannot cite documents.")
    return HistoryTurn(_text(value["question"], f"case {case_id} history question", 2000), answer, cited)


def _evidence(value: object, case_id: str) -> Evidence:
    if not isinstance(value, dict) or set(value) != {"document", "passage"}:
        raise EvaluationError(f"Case {case_id} evidence entries need exactly a document and a passage.")
    return Evidence(_text(value["document"], "evidence document", 255), _text(value["passage"], "evidence passage", 500))


def _strings(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 12:
        raise EvaluationError(f"{label} must be a list of at most 12 strings.")
    return tuple(_text(entry, label, 255) for entry in value)


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise EvaluationError(f"{label} must be a string of 1 to {limit} characters.")
    return " ".join(value.split())


# ------------------------------------------------------------------ conversation inputs


def history_and_focus(
    case: WorkloadCase, document_ids: dict[str, object], turns: int, named: Sequence[Sequence] | None = None
) -> tuple[list, list]:
    """History turns and focus document IDs, built as `ConversationService.context` builds them for a saved
    conversation: an unanswered turn is replaced by `NO_ANSWER_NOTE`; each turn's source state is the documents it
    cited, else those its question named (`named[i]`, as the product saved them); the focus is the latest turn's
    with any; only the last `turns` turns are kept."""
    from app.rag.answer import NO_ANSWER_NOTE, Turn

    history: list[Turn] = []
    focus: list = []
    for index, turn in enumerate(case.history):
        cited = list(dict.fromkeys(document_ids[name] for name in turn.cited))
        documents = cited or list(named[index] if named else [])
        if documents:
            focus = documents
        history.append(Turn(
            turn.question, turn.answer if turn.answer is not None else NO_ANSWER_NOTE, grounded=bool(turn.cited),
            documents=tuple(documents),
        ))
    return (history[-turns:] if turns else []), focus


# ------------------------------------------------------------------ scoring


def contains(text: str, fact: str) -> bool:
    """Whether the text states the fact; "a|b" accepts either alternative. Thousands separators are ignored."""
    plain = _plain(text)
    return any(_plain(option) in plain for option in fact.split("|") if option.strip())


def _plain(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", normalize(text))


def _same(filename: str, expected: str) -> bool:
    return normalize(filename) == normalize(expected)


def judge_retrieval(case: WorkloadCase, passages: Sequence, history_documents: Sequence[str]) -> dict:
    """Scores the passages `run_chat` handed to the model. Each passage needs `filename` and `content`."""
    files = [passage.filename for passage in passages]
    found = [doc for doc in case.expected_documents if any(_same(name, doc) for name in files)]
    evidence_found = [
        entry for entry in case.evidence
        if any(_same(p.filename, entry.document) and contains(p.content, entry.passage) for p in passages)
    ]
    forbidden = [
        name for name in files
        if any(_same(name, doc) for doc in case.forbidden_documents)
    ] + [fact for fact in case.forbidden_facts if case.expect != "answer" and any(contains(p.content, fact) for p in passages)]
    unexpected_history = [
        name for name in files
        if any(_same(name, doc) for doc in history_documents)
        and not any(_same(name, doc) for doc in case.expected_documents)
    ]
    distractors = [name for name in files if any(_same(name, doc) for doc in case.distractor_documents)]
    document_hit = bool(case.expected_documents) and len(found) == len(case.expected_documents)
    evidence_hit = bool(case.evidence) and len(evidence_found) == len(case.evidence)
    return {
        "passages": len(files),
        "documents": len(set(files)),
        "retrieved_files": files,
        "document_recall": round(len(found) / len(case.expected_documents), 4) if case.expected_documents else None,
        "document_hit": document_hit if case.expected_documents else None,
        "evidence_recall": round(len(evidence_found) / len(case.evidence), 4) if case.evidence else None,
        "evidence_hit": evidence_hit if case.evidence else None,
        # The incident pattern: the right document arrived without the section that answers the question.
        "document_without_evidence": (bool(found) and not evidence_hit) if case.evidence else None,
        "history_contamination": (
            round(len(unexpected_history) / len(files), 4) if files and case.independent else None
        ),
        "distractor_share": round(len(distractors) / len(files), 4) if files and case.distractor_documents else None,
        "acl_leak": bool(forbidden),
    }


def judge_answer(
    case: WorkloadCase,
    *,
    answer: str,
    insufficient: bool,
    general: bool,
    cited_files: Sequence[str],
    evidence_hit: bool | None,
    decision: str | None,
    strategy: str | None = None,
) -> dict:
    """The rubric of `app.rag.evaluation.answers` (forbidden fact, general knowledge, refusal, cited facts), plus
    whether the expectation was met, history leaks, and the stage that failed.

    A reply that says the sources do not state the fact, but is shown as a cited answer (not flagged as
    insufficient evidence), is a `cited_non_answer`: it does not mislead about the fact, so it meets a refusal
    expectation, but its citations point at passages that do not answer; it is counted separately."""
    forbidden_hits = [fact for fact in case.forbidden_facts if contains(answer, fact)]
    facts_present = bool(case.expected_facts) and all(contains(answer, fact) for fact in case.expected_facts)
    marked = bool(case.conflict_markers) and any(contains(answer, marker) for marker in case.conflict_markers)
    leaked = bool(forbidden_hits) and not (facts_present and marked)
    history_leak = [fact for fact in case.history_facts if contains(answer, fact)]
    cited_expected = [doc for doc in case.expected_documents if any(_same(name, doc) for name in cited_files)]
    all_cited = bool(case.expected_documents) and len(cited_expected) == len(case.expected_documents)

    if leaked:
        judgment = "unsupported_claim"
    elif general:
        judgment = "general_knowledge"
    elif insufficient:
        judgment = "refused"
    elif facts_present and all_cited:
        judgment = "correct"
    elif facts_present and cited_expected:
        judgment = "partially_cited"
    elif facts_present:
        judgment = "uncited"
    elif _NOT_STATED.search(answer):
        judgment = "cited_non_answer"
    elif evidence_hit and case.expected_facts:
        judgment = "uncertain"  # the evidence was there; the reply may be a paraphrase: a person reviews it
    else:
        judgment = "ungrounded"

    if case.expect == "answer":
        passed = judgment == "correct"
    elif case.expect == "refusal":
        passed = judgment in {"refused", "cited_non_answer"}
    else:  # a general question: any non-refused reply that leaks nothing
        passed = judgment in {"general_knowledge", "correct", "uncited", "ungrounded"} and not insufficient
    passed = passed and not history_leak
    return {
        "judgment": judgment,
        "passed": passed,
        "human_review": judgment == "uncertain",
        "facts_present": facts_present,
        "cited_expected_documents": len(cited_expected),
        "history_leak": bool(history_leak),
        "general_on_document_question": general and case.expect != "general",
        "failure_stage": _stage(case, judgment, passed, leaked, evidence_hit, decision, bool(history_leak), strategy),
    }


def rejudge(case: WorkloadCase, row: dict) -> dict:
    """Re-score a stored answer row (from a report) with the current rubric, without calling the provider. The
    stored judgment tells whether the reply was a refusal or general knowledge."""
    stored = row["answer"]
    judged = judge_answer(
        case, answer=stored["reply"], insufficient=stored["judgment"] == "refused",
        general=stored["judgment"] == "general_knowledge", cited_files=stored["cited_files"],
        evidence_hit=row["retrieval"]["evidence_hit"], decision=row["decision"], strategy=row["strategy"],
    )
    return {**stored, **judged}


# Replies that say the sources do not state the fact (English, Roman Urdu, Urdu).
_NOT_STATED = re.compile(
    r"(?:\bnot|n[’']t)\s+(?:state|stated|specif\w*|mention\w*|includ\w*|list\w*|given|provided|available|identif\w*)"
    r"|\bdoes(?:n[’']t| not) (?:say|contain|show)|\bno (?:information|mention|details?)\b|\bmissing\b"
    r"|\bnahi[nñ]?\s+(?:di|diya|diye|hai|hain|mila|mili|bataya|dikh)"
    r"|\b(?:di|diya|diye|gaya|gayi|bataya|likha|mila|mili)\s+nahi[nñ]?\b|\bnahi\b.*\b(?:source|document)"
    r"|نہیں (?:دی|دیا|ہے|ملا|ملی)|موجود نہیں|درج نہیں",
    re.IGNORECASE,
)


# Decisions written by `answer_question` (see `app.rag.tracing.decide`), grouped by the step that made them.
_VALIDATION = re.compile(r"^(refused|general_after)_(unsupported|inconclusive|verifier)$")
_MODEL = {"refused_model_insufficient", "refused_uncited", "refused_uncited_document_question",
          "general_after_insufficient", "general_uncited", "refused_empty"}


def _stage(
    case: WorkloadCase, judgment: str, passed: bool, leaked: bool, evidence_hit: bool | None,
    decision: str | None, history_leak: bool, strategy: str | None = None,
) -> str:
    if passed:
        return "none"
    if leaked and case.scenario == "acl":
        return "permission"
    if history_leak:
        return "history_leak"
    if judgment == "uncertain":
        return "unverified"
    if strategy == "table_analysis":
        return "table_analysis"  # the computed table result replaced the passages: its query or table was wrong
    if case.expect == "answer" and evidence_hit is False:
        return "retrieval"
    if decision and _VALIDATION.match(decision):
        return "validation"
    if decision in _MODEL:
        return "generation"
    if case.expect != "answer" and judgment == "general_knowledge":
        return "fallback"
    return "generation"


def summarize(rows: list[dict], *, answers: bool) -> dict:
    """Overall and per-scenario rates. Rates are over the cases they apply to; `None` when none apply."""
    out = {"overall": _rates(rows, answers=answers), "by_scenario": {}, "by_language": {}}
    for key, target in (("scenario", "by_scenario"), ("language", "by_language")):
        for value in sorted({row[key] for row in rows}):
            out[target][value] = _rates([row for row in rows if row[key] == value], answers=answers)
    return out


def _rates(rows: list[dict], *, answers: bool) -> dict:
    retrieval = [row["retrieval"] for row in rows]
    latencies = [row["latency_ms"] for row in rows]
    rates = {
        "cases": len(rows),
        "document_hit_rate": _rate([r["document_hit"] for r in retrieval]),
        "document_recall": _avg([r["document_recall"] for r in retrieval]),
        "evidence_hit_rate": _rate([r["evidence_hit"] for r in retrieval]),
        "document_without_evidence_rate": _rate([r["document_without_evidence"] for r in retrieval]),
        "history_contamination": _avg([r["history_contamination"] for r in retrieval]),
        "distractor_share": _avg([r["distractor_share"] for r in retrieval]),
        "acl_leaks": sum(1 for r in retrieval if r["acl_leak"]),
        "latency_p50_ms": _round(percentile(latencies, 50)),
        "latency_p95_ms": _round(percentile(latencies, 95)),
    }
    if answers:
        judged = [row["answer"] for row in rows if "answer" in row]
        measured = [a for a in judged if not a["human_review"]]
        expect = {row["id"]: row["expect"] for row in rows}
        by_id = {row["id"]: row["answer"] for row in rows if "answer" in row}
        rates.update({
            "pass_rate": _rate([a["passed"] for a in measured]),
            "answer_correctness": _rate([by_id[i]["passed"] for i in by_id if expect[i] == "answer" and not by_id[i]["human_review"]]),
            "refusal_accuracy": _rate([by_id[i]["passed"] for i in by_id if expect[i] == "refusal"]),
            "general_on_document_question": _rate([a["general_on_document_question"] for i, a in by_id.items() if expect[i] != "general"]),
            "history_leaks": sum(1 for a in judged if a["history_leak"]),
            "unsupported_claims": sum(1 for a in judged if a["judgment"] == "unsupported_claim"),
            "cited_non_answers": sum(1 for a in judged if a["judgment"] == "cited_non_answer"),
            "false_refusals": sum(
                1 for row in rows
                if "answer" in row and row["expect"] == "answer" and row["answer"]["judgment"] == "refused"
                and row["retrieval"]["evidence_hit"]
            ),
            "needs_review": sum(1 for a in judged if a["human_review"]),
            "failure_stages": _counts([a["failure_stage"] for a in judged if a["failure_stage"] != "none"]),
        })
    return rates


def _rate(values: list) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(1 for value in known if value) / len(known), 4) if known else None


def _avg(values: list) -> float | None:
    known = [value for value in values if value is not None]
    return _round(mean(known))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _counts(values: list[str]) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# ------------------------------------------------------------------ corpus


def corpus() -> dict[str, bytes]:
    """Synthetic Northwind Traders documents. Facts some cases need sit only in the filename (a month, a date, a
    site), as they do in real libraries."""
    from app.rag.evaluation.golden import _pdf

    files: dict[str, bytes] = {
        "Internet-Bills-July2026.docx": _payment_memo(
            "06.07.2026",
            [("Internet media link Nexlinx", "18,450/-"), ("Internet media link Optix 200mbps", "14,600/-"),
             ("Internet media link Optix 50mbps", "4,100/-"), ("Internet media link Optix 50mbps", "4,100/-")],
            "41,250/-",
        ),
        "Internet-Bills-August2026.docx": _payment_memo(
            "05.08.2026",
            [("Internet media link Nexlinx", "18,450/-"), ("Internet media link Optix 200mbps", "14,600/-"),
             ("Internet media link Optix 50mbps", "4,100/-"), ("Backup link StormFiber 30mbps", "10,650/-")],
            "47,800/-",
        ),
        "Internet-Bill-Sahiwal-Branch.docx": _payment_memo(
            "08.08.2026", [("Strong link Internet 100mbps", "60,000/-")], "60,000/-",
        ),
        "billing-dashboard-capture.txt": (
            "Screenshot of the Billing & payments dashboard for Northwind Traders.\n"
            "Outstanding balance: $0.00. Payment methods: none added. Payment activity: no recent spending.\n"
            "Business currency: US Dollars (USD). Menu: Accounts, Payment methods, Payment activity, Invoices.\n"
        ).encode(),
        "Leave-Policy-v3-2026.pdf": _pdf(
            "Northwind Traders Leave Policy, version 3, effective 1 January 2026. This version supersedes version 2.",
            "Annual leave: permanent employees receive 20 working days of annual leave per calendar year. "
            "Up to 7 unused days may be carried over to the next year.",
            "Sick leave: 10 days per year. A medical certificate is required for absences longer than 2 days.",
        ),
        "Leave-Policy-v2-2024-SUPERSEDED.pdf": _pdf(
            "Northwind Traders Leave Policy, version 2, 2024. Superseded by version 3 on 1 January 2026.",
            "Annual leave: permanent employees receive 16 working days of annual leave per calendar year.",
        ),
        "Vendor-Contract-CloudNet.docx": _sections(
            ("Master Services Agreement", "Agreement between Northwind Traders and CloudNet Pvt Ltd, effective 1 March 2026."),
            ("Service levels", "CloudNet guarantees 99.5% monthly uptime for the managed WAN service."),
            ("Termination", "Either party may terminate this agreement with 90 days written notice."),
            ("Fees", "The monthly fee is PKR 240,000, invoiced quarterly in advance."),
        ),
        "Vendor-Contract-DataServe.docx": _sections(
            ("Colocation Agreement", "Agreement between Northwind Traders and DataServe Solutions, effective 15 May 2026."),
            ("Service levels", "DataServe guarantees 99.9% uptime for power and cooling in the colocation hall. "
                               "Service credits are described in Schedule B."),
            ("Termination", "Either party may terminate this agreement with 30 days written notice."),
            ("Fees", "The monthly fee is PKR 180,000 per rack."),
        ),
        "Board-Minutes-2026-09-15.docx": _sections(
            ("Board of Directors", "Minutes of the quarterly board meeting held at the head office."),
            ("Decisions", "The board approved PKR 12 million for the CCTV upgrade across all sites. "
                          "The ERP migration was deferred to the second quarter of 2027."),
            ("Action items", "The CFO will present a revised capital budget at the next meeting."),
        ),
        "Board-Minutes-2026-06-10.docx": _sections(
            ("Board of Directors", "Minutes of the quarterly board meeting held at the head office."),
            ("Decisions", "The board approved PKR 4.5 million for generator replacement at the Sahiwal branch. "
                          "The hiring freeze for the IT department was lifted."),
            ("Action items", "The admin manager will collect three generator quotations by 30 June."),
        ),
        "Q3-2026-Cooling-Audit-Lahore-DC.pdf": _pdf(
            "Data centre cooling audit, Q3 2026.",
            "Findings: chiller 2 shows high vibration and needs a bearing replacement. CRAC unit 4 filters are "
            "overdue for replacement. Average cold-aisle temperature was 23.5 C.",
        ),
        "Q3-2026-Cooling-Audit-Karachi-DC.pdf": _pdf(
            "Data centre cooling audit, Q3 2026.",
            "Findings: all chillers are within tolerance. A refrigerant leak on CRAC unit 2 was repaired. "
            "Average cold-aisle temperature was 25.1 C.",
        ),
        "Generator-Maintenance-Log-Q3-2026.xlsx": _generator_log(2000),
        "Generator-Fuel-Policy.txt": (
            "Generator fuel policy\n"
            "Diesel is reordered when a tank falls to 35 percent of its capacity.\n"
            "Each site keeps a 48-hour fuel reserve.\n"
            "Fuel deliveries are logged by the site supervisor.\n"
        ).encode(),
        "Guard-Roster-Urdu.txt": (
            "سیکیورٹی گارڈ روسٹر\n"
            "رات کی شفٹ کا انچارج گارڈ عمران خان ہے۔\n"
            "دن کی شفٹ کا انچارج گارڈ ساجد علی ہے۔\n"
            "شفٹ کی تبدیلی صبح آٹھ بجے ہوتی ہے۔\n"
        ).encode("utf-8"),
        "Cafeteria-Notice-RomanUrdu.txt": (
            "Cafeteria notice\n"
            "Cafeteria ka lunch time dopehar 1:30 se 2:30 tak hai.\n"
            "Jumma ko lunch 2:15 se 3:15 tak hoga.\n"
            "Tea break shaam 5:00 baje hai.\n"
        ).encode(),
        "Visitor-Parking-Notice.docx": _parking_notice(),
        "Salary-Revision-2026-Confidential.xlsx": _salary_sheet(),
        "CEO-Private-Notes.txt": (
            "Personal notes\n"
            "Acquisition target code ORCHID-7. The shortlist meeting is on 3 November.\n"
        ).encode(),
    }
    return files


def _docx():
    import docx

    return docx.Document()


def _save(document) -> bytes:
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def _payment_memo(date: str, rows: list[tuple[str, str]], total: str) -> bytes:
    """An inter-office payment memo laid out like a real one: header lines, a table, a total, a signature block.
    The words "bill" and the month do not appear in it: only the filename names them."""
    document = _docx()
    for line in (
        "Northwind Traders", "Inter Office Memo", "From: IT Department", "To: CEO, Director Operations and CFO",
        "Subject: Agenda for Payments Approval.", f"Date: {date}",
        "We are pleased to inform you that the following payments are required for approval.",
    ):
        document.add_paragraph(line)
    table = document.add_table(rows=len(rows) + 2, cols=3)
    cells = [("Sr. No", "Description", "Amount (Rs.)")]
    cells += [(str(i), description, amount) for i, (description, amount) in enumerate(rows, start=1)]
    cells += [("", "Total", total)]
    for row, values in zip(table.rows, cells, strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    for line in ("Your approval is solicited please.", "Initiated by (HOD IT)          CFO",
                 "Director Operations          CEO"):
        document.add_paragraph(line)
    return _save(document)


def _sections(*sections: tuple[str, str]) -> bytes:
    document = _docx()
    for heading, body in sections:
        document.add_heading(heading, level=1)
        document.add_paragraph(body)
    return _save(document)


def _parking_notice() -> bytes:
    document = _docx()
    document.add_heading("Visitor parking", level=1)
    document.add_paragraph("Effective 1 October 2026.")
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(
        table.rows,
        (("Area", "Location", "Slots"), ("Visitors", "Basement level B2", "10"), ("Deliveries", "Rear gate bay", "3")),
        strict=True,
    ):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    document.add_paragraph("Contact the front desk for a parking token.")
    return _save(document)


def _generator_log(rows: int) -> bytes:
    """A large, repetitive log that shares the words "generator", "fuel" and "site" with the short fuel policy."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Q3 log"
    sheet.append(["Date", "Site", "Generator", "Run hours", "Fuel level (%)", "Remarks"])
    sites = ("Lahore head office", "Sahiwal branch", "Karachi DC", "Lahore DC")
    for n in range(rows):
        sheet.append([
            f"2026-{7 + n % 3:02d}-{1 + n % 28:02d}", sites[n % 4], f"GEN-{1 + n % 6}", 2 + n % 5,
            60 + n % 35, "Routine generator check; fuel level normal; no issue at the site",
        ])
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


def _salary_sheet() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revisions"
    sheet.append(["Grade", "Current salary", "Revised salary 2026"])
    sheet.append(["Manager", "PKR 350,000", "PKR 385,000"])
    sheet.append(["Engineer", "PKR 220,000", "PKR 245,000"])
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


async def seed(sessions, settings, rag) -> int:
    """Upload and process the workload corpus. Run after `golden.seed`, which creates the personas."""
    from sqlalchemy import select
    from starlette.datastructures import UploadFile

    from app.db.models import Document, User
    from app.rag.documents import DocumentService
    from app.rag.evaluation.golden import PERSONAS
    from app.services.access_control import AccessAdminService
    from app.services.audit import AuditContext

    ctx = AuditContext(None, "enterprise-workload-eval", None, None)
    files = corpus()

    async def user(db, persona: str):
        return (await db.execute(select(User).where(User.email == PERSONAS[persona]))).scalar_one()

    async def upload(persona: str, filename: str, visibility: str):
        async with sessions() as db:
            service = DocumentService(db, settings, rag.storage, rag.indexer, await user(db, persona), ctx)
            return (await service.upload(UploadFile(filename=filename, file=BytesIO(files[filename])), visibility)).id

    async with sessions() as db:
        access = AccessAdminService(db, await user(db, "admin"), ctx)
        group = await access.create_group(HR_GROUP, "project", None)
        await access.add_member(group.id, PERSONAS["member"])
        group_id = group.id
    for filename in files:
        if filename not in RESTRICTED and filename not in PRIVATE:
            await upload("admin", filename, "shared")
    for filename, persona in PRIVATE.items():
        await upload(persona, filename, "private")
    for filename in RESTRICTED:
        doc_id = await upload("admin", filename, "private")
        async with sessions() as db:
            service = DocumentService(db, settings, rag.storage, rag.indexer, await user(db, "admin"), ctx)
            await service.set_access(doc_id, "restricted", [], [group_id])
    while await rag.worker.process_next():
        pass
    async with sessions() as db:
        rows = (await db.execute(
            select(Document.filename, Document.status, Document.error_code).where(Document.filename.in_(list(files)))
        )).all()
    failed = [f"{row.filename} ({row.status} {row.error_code or ''})".strip() for row in rows if row.status != "ready"]
    if failed or len(rows) != len(files):
        raise RuntimeError(f"Workload documents did not become ready: {', '.join(failed) or 'some are missing'}")
    return len(files)
