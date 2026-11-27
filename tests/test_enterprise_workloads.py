"""Enterprise workload evaluation: case format, conversation inputs, scoring rules and corpus properties.

No provider and no retrieval: the runner (`evals/run_enterprise_workloads.py`) measures those. These tests pin the
scoring so a baseline and a later run are judged by the same rules.
"""

import json
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest

from app.rag.answer import NO_ANSWER_NOTE
from app.rag.evaluation import workloads
from app.rag.evaluation.dataset import EvaluationError
from app.rag.evaluation.workloads import (
    HistoryTurn,
    WorkloadCase,
    contains,
    history_and_focus,
    judge_answer,
    judge_retrieval,
    load_workloads,
)

DATASET = Path(__file__).resolve().parents[1] / "evals" / "enterprise_workloads.json"


@dataclass(frozen=True)
class _Passage:
    filename: str
    content: str


def _case(**overrides) -> WorkloadCase:
    values = {
        "id": "case", "scenario": "discovery", "language": "en", "persona": "admin", "question": "q",
        "expect": "answer", "expected_documents": ("July.docx",),
        "evidence": (workloads.Evidence("July.docx", "41,250"),), "expected_facts": ("41,250",),
    }
    values.update(overrides)
    return WorkloadCase(**values)


def _write(tmp_path: Path, cases: list[dict]) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"name": "suite", "cases": cases}), encoding="utf-8")
    return path


def test_shipped_suite_loads_and_covers_every_scenario():
    suite = load_workloads(DATASET)
    assert {case.scenario for case in suite.cases} == set(workloads.SCENARIOS)
    corpus = workloads.corpus()
    named = {
        name
        for case in suite.cases
        for name in (*case.expected_documents, *case.forbidden_documents, *case.distractor_documents)
    } | {name for case in suite.cases for turn in case.history for name in turn.cited}
    assert named <= set(corpus)


@pytest.mark.parametrize(
    "case, message",
    [
        ({"expected_facts": []}, "needs expected_documents and expected_facts"),
        ({"expect": "refusal"}, "cannot list expected_documents"),
        ({"evidence": [{"document": "Other.docx", "passage": "x"}]}, "evidence names documents it does not expect"),
        ({"surprise": 1}, "unknown fields"),
        ({"independent": True}, "independent of a history"),
        ({"history": [{"question": "q", "answer": None, "cited": ["July.docx"]}]}, "cannot cite"),
    ],
)
def test_invalid_cases_are_rejected(tmp_path, case, message):
    base = {
        "id": "c1", "scenario": "discovery", "language": "en", "persona": "admin", "expect": "answer",
        "question": "q", "expected_documents": ["July.docx"], "expected_facts": ["41,250"],
    }
    with pytest.raises(EvaluationError, match=message):
        load_workloads(_write(tmp_path, [{**base, **case}]))


def test_contains_accepts_alternatives_and_thousands_separators():
    assert contains("Total Rs. 41250", "41,250")
    assert contains("کل رقم بیس دن", "20|بیس")
    assert not contains("Total 47,800", "41,250")


def test_history_and_focus_match_saved_conversations():
    july, dashboard = uuid.uuid4(), uuid.uuid4()
    case = _case(history=(
        HistoryTurn("dashboard?", "Balance $0.00 [S1]", ("dash.txt",)),
        HistoryTurn("july ka bill?", None),
        HistoryTurn("July total?", "Rs. 41,250 [S1]", ("July.docx",)),
        HistoryTurn("net ka bill?", None),
    ))
    history, focus = history_and_focus(case, {"July.docx": july, "dash.txt": dashboard}, turns=3)
    assert [turn.question for turn in history] == ["july ka bill?", "July total?", "net ka bill?"]
    assert history[0].answer == NO_ANSWER_NOTE
    assert focus == [july]  # the most recent citing answer, as `ConversationService.context` keeps it


def test_document_found_without_its_answer_bearing_section():
    result = judge_retrieval(_case(), [_Passage("July.docx", "Subject: Agenda for Payments Approval.")], [])
    assert result["document_hit"] is True
    assert result["evidence_hit"] is False
    assert result["document_without_evidence"] is True


def test_history_contamination_and_acl_leaks():
    case = _case(
        history=(HistoryTurn("dashboard?", "Balance $0.00", ("dash.txt",)),), independent=True,
        forbidden_documents=("Salary.xlsx",),
    )
    passages = [_Passage("dash.txt", "Outstanding balance: $0.00"), _Passage("July.docx", "Total 41,250/-"),
                _Passage("Salary.xlsx", "Manager PKR 385,000")]
    result = judge_retrieval(case, passages, ["dash.txt"])
    assert result["history_contamination"] == round(1 / 3, 4)
    assert result["acl_leak"] is True


def test_general_answer_to_a_document_question_fails_and_is_attributed():
    case = _case(history_facts=("0.00",))
    judged = judge_answer(
        case, answer="The dashboard shows $0.00 outstanding.", insufficient=False, general=True, cited_files=[],
        evidence_hit=True, decision="general_after_unsupported",
    )
    assert judged["passed"] is False
    assert judged["general_on_document_question"] is True
    assert judged["history_leak"] is True
    assert judged["failure_stage"] == "history_leak"


def test_validation_rejection_is_separated_from_retrieval_misses():
    rejected = judge_answer(
        _case(), answer="I can't verify that.", insufficient=True, general=False, cited_files=[],
        evidence_hit=True, decision="refused_unsupported",
    )
    missed = judge_answer(
        _case(), answer="I can't verify that.", insufficient=True, general=False, cited_files=[],
        evidence_hit=False, decision="refused_unsupported",
    )
    assert rejected["failure_stage"] == "validation"
    assert missed["failure_stage"] == "retrieval"


def test_multi_document_answers_must_cite_every_document():
    case = _case(expected_documents=("July.docx", "August.docx"), evidence=(), expected_facts=("41,250", "47,800"))
    partial = judge_answer(
        case, answer="July 41,250 [S1], August 47,800", insufficient=False, general=False,
        cited_files=["July.docx"], evidence_hit=None, decision="supported",
    )
    full = judge_answer(
        case, answer="July 41,250 [S1], August 47,800 [S2]", insufficient=False, general=False,
        cited_files=["July.docx", "August.docx"], evidence_hit=None, decision="supported",
    )
    assert partial["judgment"] == "partially_cited" and not partial["passed"]
    assert full["judgment"] == "correct" and full["passed"]


def test_cited_non_answer_meets_a_refusal_but_not_an_answer():
    reply = "The sources do not identify an acquisition target code. [S1]"
    refusal = judge_answer(
        _case(expect="refusal", expected_documents=(), evidence=(), expected_facts=()), answer=reply,
        insufficient=False, general=False, cited_files=["asset-register.xlsx"], evidence_hit=None, decision="verified",
    )
    answer = judge_answer(
        _case(), answer="July ka total source mein diya nahi gaya. [S1]", insufficient=False, general=False,
        cited_files=["July.docx"], evidence_hit=False, decision="supported",
    )
    assert refusal["judgment"] == "cited_non_answer" and refusal["passed"]
    assert answer["judgment"] == "cited_non_answer" and answer["failure_stage"] == "retrieval"


def test_table_route_failures_are_attributed_to_the_table_step():
    judged = judge_answer(
        _case(), answer="Total missing. [S1]", insufficient=False, general=False, cited_files=["July.docx"],
        evidence_hit=False, decision="supported", strategy="table_analysis",
    )
    assert judged["failure_stage"] == "table_analysis"


def test_superseded_value_is_excused_only_when_marked():
    case = _case(expected_facts=("20",), forbidden_facts=("16",), conflict_markers=("supersed",))
    marked = judge_answer(
        case, answer="20 days [S1]; version 2 (superseded) said 16.", insufficient=False, general=False,
        cited_files=["July.docx"], evidence_hit=True, decision="supported",
    )
    unmarked = judge_answer(
        case, answer="16 days [S1].", insufficient=False, general=False, cited_files=["July.docx"],
        evidence_hit=True, decision="supported",
    )
    assert marked["passed"]
    assert unmarked["judgment"] == "unsupported_claim"


def test_corpus_keeps_month_site_and_date_only_in_filenames():
    """The discovery cases rely on this: the month, site or date is in the filename, not in the text."""
    import docx
    from pypdf import PdfReader

    def text(name: str, data: bytes) -> str:
        if name.endswith(".docx"):
            document = docx.Document(BytesIO(data))
            cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
            return "\n".join([p.text for p in document.paragraphs] + cells)
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)

    files = workloads.corpus()
    for name, absent in {
        "Internet-Bills-July2026.docx": ("july", "bill"),
        "Internet-Bill-Sahiwal-Branch.docx": ("sahiwal",),
        "Board-Minutes-2026-09-15.docx": ("september", "15"),
        "Q3-2026-Cooling-Audit-Lahore-DC.pdf": ("lahore",),
    }.items():
        body = text(name, files[name]).casefold()
        assert not any(word in body for word in absent), name
