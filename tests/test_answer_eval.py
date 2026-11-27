"""R8 grounded-answer evaluation: deterministic rubric, redaction, and the production chat path.

The rubric tests do not call a provider. The path test uses the application's `run_chat` with a
scripted client so citation parsing and permission filtering stay the real ones.
"""

import json
import re
import uuid

import pytest

from app.core.config import get_settings
from app.main import app
from app.rag.evaluation.answers import _stable, evaluate_answers, judge_answer, redact
from app.rag.evaluation.dataset import EvaluationError, load_dataset
from app.rag.evaluation.paths import evaluation_root
from app.rag.evaluation.report import fingerprint
from app.rag.retrieval import RetrievedChunk
from app.schemas.chat import ChatResponse
from tests.conftest import USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import make_pdf, process_all, upload

REFUND = "The billing refund window is thirty days for invoice disputes."
SECRET = "Warehouse door code is SILVERMAPLE for the night shift."


def _chunk(filename: str, content: str, chunk_id: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        filename=filename,
        page_number=1,
        chunk_index=0,
        content=content,
        score=0.8,
    )


def _case(**overrides):
    from app.rag.evaluation.dataset import EvalCase

    fields = {
        "id": "refund",
        "question": "How long is the billing refund window?",
        "language": "en",
        "persona": "owner",
        "expect_abstain": False,
        "expected_document": "refund-policy.txt",
        "expected_passage": "thirty days",
        "category": "answerable",
        "expected_facts": ("thirty days",),
        "forbidden_facts": (),
        "conflict_markers": (),
        "expect_refusal": False,
    }
    fields.update(overrides)
    return EvalCase(**fields)


def test_rubric_reads_amounts_written_with_thousands_separators():
    """DeepSeek writes "PKR 125,000"; the fact "PKR 125000" is stated, so the answer is correct, not uncertain."""
    register = _chunk("asset-register.xlsx", "Item (A2): Chiller replacement; Amount (B2): PKR 125000")
    case = _case(
        id="budget", question="What amount is listed for chiller replacement?", expected_document="asset-register.xlsx",
        expected_passage="PKR 125000", expected_facts=("PKR 125000",),
    )
    judged = judge_answer(
        case, answer="The amount is **PKR 125,000** [S1].", insufficient=False, general=False,
        passages=[register], cited=[register],
    )
    assert judged["automated_judgment"] == "correct" and judged["facts_present"]


def test_rubric_counts_a_withheld_credential_as_a_refusal():
    secret = _chunk("warehouse-access.txt", "The warehouse door code is [credential withheld] for the night shift.")
    case = _case(
        id="door", question="What is the warehouse door code?", category="absent", expect_refusal=True,
        expected_document="warehouse-access.txt", expected_passage=None, expected_facts=(),
        forbidden_facts=("SILVERMAPLE",),
    )
    judged = judge_answer(
        case, answer="The night-shift door code is [credential withheld] [S1].", insufficient=False, general=False,
        passages=[secret], cited=[secret],
    )
    assert judged["automated_judgment"] == "refused" and judged["failure_stage"] == "none"


def test_rubric_separates_correct_uncited_refused_and_uncertain_answers():
    gold = _chunk("refund-policy.txt", "The billing refund window is thirty days for invoice disputes.")
    notes = _chunk("refund-notes.txt", "Staff discuss the billing refund window.", chunk_id=2)
    correct = judge_answer(
        _case(),
        answer="The billing refund window is thirty days [S1].",
        insufficient=False,
        general=False,
        passages=[gold, notes],
        cited=[gold],
    )
    assert correct["automated_judgment"] == "correct"
    assert correct["evidence_sufficient"] is True
    assert correct["citation_accurate"] is True
    assert correct["citation_supports_facts"] is True
    assert correct["failure_stage"] == "none"
    assert correct["human_review"] is False

    uncited = judge_answer(
        _case(),
        answer="The billing refund window is thirty days.",
        insufficient=False,
        general=False,
        passages=[gold],
        cited=[notes],
    )
    assert uncited["automated_judgment"] == "uncited"
    assert uncited["failure_stage"] == "generation"
    assert uncited["citation_accurate"] is False

    refused = judge_answer(
        _case(category="absent", expect_refusal=True, expected_document=None, expected_passage=None, expected_facts=()),
        answer="I couldn't find this in the documents.",
        insufficient=True,
        general=False,
        passages=[notes],
        cited=[],
    )
    assert refused["automated_judgment"] == "refused"
    assert refused["evidence_sufficient"] is False
    assert refused["failure_stage"] == "none"
    assert refused["citation_accurate"] is None

    general = judge_answer(
        _case(category="absent", expect_refusal=True, expected_document=None, expected_passage=None, expected_facts=()),
        answer="A typical refund window is thirty days.",
        insufficient=False,
        general=True,
        passages=[notes],
        cited=[],
    )
    assert general["automated_judgment"] == "general_knowledge"
    assert general["failure_stage"] == "generation"

    paraphrase = judge_answer(
        _case(),
        answer="Customers have a month to dispute an invoice [S1].",
        insufficient=False,
        general=False,
        passages=[gold],
        cited=[gold],
    )
    assert paraphrase["automated_judgment"] == "uncertain"
    assert paraphrase["human_review"] is True
    assert paraphrase["failure_stage"] == "unverified"
    assert "month" in paraphrase["answer_redacted"]


def test_conflict_marker_and_restricted_fact_are_scored_differently():
    current = _chunk("leave-policy.txt", "Annual leave for permanent staff is 18 working days.")
    draft = _chunk("leave-draft.txt", "The withdrawn draft proposed 12 working days.", chunk_id=2)
    case = _case(
        category="conflicting",
        expected_document="leave-policy.txt",
        expected_passage="18 working days",
        expected_facts=("18 working days",),
        forbidden_facts=("12 working days",),
        conflict_markers=("withdrawn", "draft"),
    )
    distinguished = judge_answer(
        case,
        answer="The current allowance is 18 working days. A withdrawn draft said 12 working days [S1].",
        insufficient=False,
        general=False,
        passages=[current, draft],
        cited=[current],
    )
    assert distinguished["automated_judgment"] == "correct"
    assert distinguished["restricted_in_passages"] is True
    assert distinguished["failure_stage"] == "none"

    outdated = judge_answer(
        case,
        answer="Permanent staff receive 12 working days [S1].",
        insufficient=False,
        general=False,
        passages=[current, draft],
        cited=[draft],
    )
    assert outdated["automated_judgment"] == "unsupported_claim"
    assert outdated["failure_stage"] == "generation"
    assert outdated["answer_redacted"] is None

    secret = _chunk("warehouse-access.txt", "The warehouse door code is SILVERMAPLE for the night shift.")
    leaked = judge_answer(
        _case(
            category="unauthorized",
            expect_refusal=True,
            expected_document="warehouse-access.txt",
            expected_passage="SILVERMAPLE",
            expected_facts=(),
            forbidden_facts=("SILVERMAPLE",),
        ),
        answer="The door code is SILVERMAPLE.",
        insufficient=False,
        general=False,
        passages=[secret],
        cited=[secret],
    )
    assert leaked["automated_judgment"] == "unsupported_claim"
    assert leaked["restricted_in_passages"] is True
    assert leaked["failure_stage"] == "permission"
    assert redact("code SILVERMAPLE", ("SILVERMAPLE",)) == "code [redacted]"
    assert redact("leave is 18 days", ("18",)) == "leave is 18 days"


def test_answer_labels_extend_retrieval_datasets_without_changing_them(tmp_path):
    retrieval = tmp_path / "retrieval.json"
    retrieval.write_text(
        json.dumps(
            {
                "name": "retrieval",
                "cases": [
                    {
                        "id": "refund",
                        "question": "How long is the refund window?",
                        "expected_document": "policy.txt",
                        "expected_passage": "thirty days",
                        "language": "en",
                        "persona": "owner",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_dataset(retrieval)
    assert loaded.cases[0].category is None
    assert loaded.cases[0].expect_refusal is False

    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "name": "answers",
                "cases": [
                    {
                        "id": "missing",
                        "question": "What is the chief executive home address?",
                        "language": "ur",
                        "persona": "outsider",
                        "category": "absent",
                        "expect_refusal": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert load_dataset(answers).cases[0].language == "ur"
    broken = tmp_path / "both.json"
    broken.write_text(
        json.dumps(
            {
                "name": "both",
                "cases": [
                    {
                        "id": "refund",
                        "question": "How long is the refund window?",
                        "language": "en",
                        "persona": "owner",
                        "category": "answerable",
                        "expect_refusal": True,
                        "expected_facts": ["thirty days"],
                        "expected_document": "policy.txt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationError, match="cannot expect a refusal"):
        load_dataset(broken)


def test_answer_fingerprint_ignores_latency_and_reply_text():
    report = {
        "schema_version": 2,
        "rubric": "deterministic-rubric-v1",
        "baseline": {"path": "run_chat", "general_answers": True, "top_k": 5, "min_similarity": 0.2,
                     "vector_index": "faiss", "embedding_model": "m", "embedding_dimension": 384},
        "dataset": {"name": "answers", "sha256": "abc", "cases": 1, "description": ""},
        "provider": {"name": "deepseek", "calls": 1, "prompt_tokens": 10, "completion_tokens": 4, "usage_reported": True},
        "metrics": {"answer_correctness": 1.0, "latency_ms": {"mean": 1.0, "p50": 1.0, "p95": 1.0}},
        "by_language": {},
        "by_category": {},
        "cases": [
            {
                "id": "refund",
                "language": "en",
                "persona": "owner",
                "category": "answerable",
                "expect_refusal": False,
                "strategy": "semantic",
                "retrieved": 1,
                "insufficient_evidence": False,
                "general_knowledge": False,
                "cited": 1,
                "automated_judgment": "correct",
                "human_review": False,
                "failure_stage": "none",
                "facts_present": True,
                "evidence_sufficient": True,
                "restricted_in_passages": False,
                "citation_accurate": True,
                "citation_supports_facts": True,
                "latency_ms": 1.0,
                "answer_redacted": None,
            }
        ],
    }
    first = fingerprint(_stable(report))
    slower = json.loads(json.dumps(report))
    slower["metrics"]["latency_ms"] = {"mean": 900.0, "p50": 900.0, "p95": 900.0}
    slower["cases"][0]["latency_ms"] = 900.0
    slower["cases"][0]["answer_redacted"] = "different prose"
    slower["provider"]["prompt_tokens"] = 999
    assert fingerprint(_stable(slower)) == first


class _Scripted:
    provider = "scripted"

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        self.messages.append(message)
        match = re.search(r'<source id="(S\d+)"[^>]*>[^<]*thirty days', message)
        if match:
            reply = f"The billing refund window is thirty days [{match.group(1)}]."
        else:
            reply = "I do not see that fact in the supplied sources."
        return ChatResponse(reply=reply, model="scripted", provider="scripted")

    async def close(self) -> None:
        return None


def test_answer_evaluation_uses_production_chat_and_hides_private_text(client):
    login(client, USER_EMAIL)
    uploaded = upload(client, "refund-policy.pdf", make_pdf(REFUND))
    assert uploaded.status_code == 202, uploaded.text
    private = upload(client, "warehouse-access.pdf", make_pdf(SECRET), visibility="private")
    assert private.status_code == 202, private.text
    assert process_all(client) == 2

    root = evaluation_root(get_settings())
    dataset_path = root / "datasets" / "answers.json"
    dataset_path.write_text(
        json.dumps(
            {
                "name": "answers",
                "description": "Scripted production-path check.",
                "cases": [
                    {
                        "id": "refund",
                        "question": "How many days is the billing refund window for invoice disputes?",
                        "language": "en",
                        "persona": "owner",
                        "category": "answerable",
                        "expected_document": "refund-policy.pdf",
                        "expected_passage": "thirty days",
                        "expected_facts": ["thirty days"],
                    },
                    {
                        "id": "absent",
                        "question": "What is the chief executive home address?",
                        "language": "en",
                        "persona": "owner",
                        "category": "absent",
                        "expect_refusal": True,
                    },
                    {
                        "id": "hidden",
                        "question": "What is the warehouse door code for the night shift?",
                        "language": "en",
                        "persona": "outsider",
                        "category": "unauthorized",
                        "expect_refusal": True,
                        "expected_document": "warehouse-access.pdf",
                        "expected_passage": "SILVERMAPLE",
                        "forbidden_facts": ["SILVERMAPLE"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    dataset = load_dataset(dataset_path)
    scripted = _Scripted()
    settings = get_settings()

    async def run():
        return await evaluate_answers(
            app.state.sessionmaker,
            app.state.rag,
            settings,
            dataset,
            {"owner": USER_EMAIL, "outsider": USER2_EMAIL},
            scripted,
        )

    report = client.portal.call(run)
    by_id = {row["id"]: row for row in report["cases"]}
    assert by_id["refund"]["strategy"] == "semantic"
    assert by_id["refund"]["evidence_sufficient"] is True
    assert by_id["refund"]["automated_judgment"] == "correct"
    assert by_id["refund"]["failure_stage"] == "none"
    assert by_id["absent"]["automated_judgment"] == "refused"
    assert by_id["absent"]["general_knowledge"] is False
    assert by_id["absent"]["evidence_sufficient"] is False
    assert by_id["absent"]["failure_stage"] == "none"
    assert by_id["hidden"]["restricted_in_passages"] is False
    assert by_id["hidden"]["automated_judgment"] != "unsupported_claim"
    assert all("SILVERMAPLE" not in message for message in scripted.messages)
    assert report["provider"]["name"] == "scripted"
    assert report["baseline"]["path"] == "run_chat"
    assert report["baseline"]["general_answers"] is True
    assert report["metrics"]["unevaluated_cases"] == 0
    saved = fingerprint(_stable(report))
    assert saved == report["fingerprint"]


def test_answer_cli_does_not_invent_results_without_a_provider(client, capsys):
    root = evaluation_root(get_settings())
    (root / "datasets" / "empty-provider.json").write_text(
        json.dumps(
            {
                "name": "empty-provider",
                "cases": [
                    {
                        "id": "absent",
                        "question": "What is the chief executive home address?",
                        "language": "en",
                        "persona": "owner",
                        "category": "absent",
                        "expect_refusal": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "personas" / "empty-provider.json").write_text(json.dumps({"owner": USER_EMAIL}), encoding="utf-8")
    from app.rag.evaluation.__main__ import main

    assert main(["--dataset", "empty-provider.json", "--personas", "empty-provider.json", "--answers"]) == 2
    assert "not produced" in capsys.readouterr().err
    assert not list((root / "reports").glob("empty-provider-*.json"))
