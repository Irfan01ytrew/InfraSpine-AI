"""R6 retrieval evaluation: metric maths, multilingual gold cases, ACL-aware retrieval and report reproducibility.

Runs against real PostgreSQL. Document text is synthetic. The runner calls `retrieve`, so a persona
only sees chunks that persona could see in chat.
"""

import json
import math
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.main import app
from app.rag.evaluation.dataset import EvaluationError, load_dataset
from app.rag.evaluation.metrics import (
    citation_precision,
    first_relevant_rank,
    ndcg_at_k,
    passage_matches,
    percentile,
    recall_at_k,
    reciprocal_rank,
)
from app.rag.evaluation.paths import evaluation_root, resolve_inside
from app.rag.evaluation.report import fingerprint, render_json, write_report
from app.rag.evaluation.runner import _stable, evaluate
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import make_pdf, process_all, upload
from tests.test_access_control import new_group, set_access

PRIVATE = "Warehouse door code is SILVERMAPLE for the night shift."
REFUND = "Billing refund window is thirty days for invoice disputes."


def test_ranking_metrics_match_hand_calculation():
    ranked = [False, True, False]
    assert recall_at_k(ranked, 1, 1) == 0
    assert recall_at_k(ranked, 5, 1) == 1
    assert reciprocal_rank(ranked) == 0.5
    assert first_relevant_rank(ranked) == 2
    assert ndcg_at_k(ranked, 5, 1) == pytest.approx(1 / math.log2(3))
    assert citation_precision(ranked) == pytest.approx(1 / 3)
    assert citation_precision([]) == 0
    both = [True, False, True]
    dcg = 1 + 1 / math.log2(4)
    idcg = 1 + 1 / math.log2(3)
    assert ndcg_at_k(both, 5, 2) == pytest.approx(dcg / idcg)
    assert recall_at_k(both, 5, 2) == 1
    assert percentile([10, 20, 30, 40], 95) == 40
    assert ndcg_at_k(ranked, 5, 0) == 0


def test_passages_match_in_english_urdu_and_roman_urdu():
    assert passage_matches("Customers may request a refund within 30 days.", "30 days")
    assert passage_matches("گاہک تیس دن کے اندر رقم واپس مانگ سکتا ہے۔", "تیس دن کے اندر")
    assert passage_matches("Refund tees din ke andar milta hai.", "refund tees din")
    assert not passage_matches("Office hours are nine to five.", "تیس دن")
    assert not passage_matches("Office hours are nine to five.", "refund tees din")


def test_dataset_accepts_three_languages_and_rejects_invalid_cases(tmp_path):
    path = tmp_path / "multilingual.json"
    path.write_text(
        json.dumps(
            {
                "name": "multilingual",
                "description": "Synthetic gold cases.",
                "cases": [
                    {
                        "id": "refund-en",
                        "question": "How long is the refund window?",
                        "expected_document": "policy.pdf",
                        "expected_passage": "30 days",
                        "language": "en",
                        "persona": "owner",
                    },
                    {
                        "id": "refund-ur",
                        "question": "رقم کی واپسی کتنے دن میں ہوتی ہے؟",
                        "expected_document": "policy-ur.pdf",
                        "expected_passage": "تیس دن کے اندر",
                        "language": "ur",
                        "persona": "owner",
                    },
                    {
                        "id": "refund-roman",
                        "question": "refund kitne din mein milta hai?",
                        "expected_document": "policy.pdf",
                        "expected_passage": "tees din",
                        "language": "roman_ur",
                        "persona": "member",
                    },
                    {
                        "id": "hidden",
                        "question": "What is the warehouse code?",
                        "expected_document": "private.pdf",
                        "expected_passage": "SILVERMAPLE",
                        "language": "en",
                        "persona": "outsider",
                        "expect_abstain": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dataset = load_dataset(path)
    assert [case.language for case in dataset.cases] == ["en", "ur", "roman_ur", "en"]
    assert dataset.cases[1].question.startswith("رقم")
    rendered = render_json({"question": dataset.cases[1].question})
    assert "رقم کی واپسی" in rendered and "\\u" not in rendered

    path.write_text(path.read_text(encoding="utf-8").replace('"ur"', '"fr"'), encoding="utf-8")
    with pytest.raises(EvaluationError, match="language"):
        load_dataset(path)

    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps(
            {
                "name": "broken",
                "cases": [
                    {"id": "no-gold", "question": "Where is the policy?", "language": "en", "persona": "owner"}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationError, match="expected document or passage"):
        load_dataset(broken)


def test_reports_are_reproducible_and_stay_inside_the_deployment(tmp_path):
    report = _sample_report()
    report["fingerprint"] = fingerprint(_stable(report))
    slower = dict(report)
    slower["metrics"] = dict(report["metrics"], latency_ms={"mean": 99.0, "p50": 99.0, "p95": 99.0})
    slower["cases"] = [{**report["cases"][0], "latency_ms": 99.0}]
    assert fingerprint(_stable(slower)) == report["fingerprint"]

    root = evaluation_root(get_settings())
    json_path, text_path = write_report(root, "sample", report)
    assert json_path.is_relative_to(root) and text_path.is_relative_to(root)
    assert json_path.parent == root / "reports"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["fingerprint"] == report["fingerprint"]
    assert saved["baseline"]["vector_index"] == "faiss"
    assert "FAISS +" in text_path.read_text(encoding="utf-8")
    with pytest.raises(EvaluationError, match="data/evaluation"):
        resolve_inside(root, tmp_path / "other-customer.json")


def test_acl_personas_use_the_same_retrieval_path(client):
    login(client, USER_EMAIL)
    uploaded = upload(client, "owner-only.pdf", make_pdf(PRIVATE))
    assert uploaded.status_code == 202, uploaded.text
    process_all(client)

    login(client, ADMIN_EMAIL)
    refunds = upload(client, "refunds.pdf", make_pdf(REFUND))
    assert refunds.status_code == 202, refunds.text
    process_all(client)
    group = new_group(client, "Billing eval")
    assert client.post(f"/api/admin/access/groups/{group}/members", json={"email": USER2_EMAIL}).status_code == 204
    assert set_access(client, refunds.json()["id"], "restricted", groups=[group]).status_code == 200

    root = evaluation_root(get_settings())
    dataset_path = root / "datasets" / "acl.json"
    persona_path = root / "personas" / "acl.json"
    dataset_path.write_text(_acl_dataset(), encoding="utf-8")
    persona_path.write_text(
        json.dumps(
            {
                "private_owner": USER_EMAIL,
                "admin": ADMIN_EMAIL,
                "member": USER2_EMAIL,
                "outsider": USER_EMAIL,
            }
        ),
        encoding="utf-8",
    )
    settings = get_settings()
    dataset = load_dataset(dataset_path)
    personas = json.loads(persona_path.read_text(encoding="utf-8"))

    async def run():
        return await evaluate(
            app.state.sessionmaker,
            app.state.rag.embedder,
            app.state.rag.vector_store,
            dataset,
            personas,
            ks=(1, 3),
            min_similarity=settings.rag_min_similarity,
            max_context_chars=settings.rag_max_context_chars,
        )

    first = client.portal.call(run)
    second = client.portal.call(run)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["baseline"] == {
        "vector_index": "faiss",
        "embedding_model": "test/fake-bow-embedder",
        "embedding_dimension": 384,
    }
    by_id = {row["id"]: row for row in first["cases"]}
    assert by_id["private-owner"]["first_relevant_rank"] == 1
    assert by_id["private-owner"]["citation_precision"] == 1
    assert by_id["private-admin"]["retrieved"] == 0 and by_id["private-admin"]["abstention_correct"] is True
    assert by_id["restricted-member"]["first_relevant_rank"] == 1
    assert by_id["restricted-member"]["language"] == "roman_ur"
    assert by_id["restricted-outsider"]["retrieved"] == 0
    assert by_id["restricted-outsider"]["abstention_correct"] is True
    assert first["metrics"]["abstention_accuracy"] == 1
    assert first["metrics"]["recall@1"] == 1
    assert app.state.rag.knowledge_store.count() == 0
    json_path, _text_path = write_report(root, dataset.name, first)
    assert json.loads(json_path.read_text(encoding="utf-8"))["fingerprint"] == first["fingerprint"]


def test_cli_runs_against_a_deployment_dataset(client, capsys):
    login(client, USER_EMAIL)
    uploaded = upload(client, "owner-only.pdf", make_pdf(PRIVATE))
    assert uploaded.status_code == 202, uploaded.text
    process_all(client)
    root = evaluation_root(get_settings())
    (root / "datasets" / "cli.json").write_text(
        json.dumps(
            {
                "name": "cli",
                "cases": [
                    {
                        "id": "owner-sees",
                        "question": "What is the SILVERMAPLE warehouse door code?",
                        "expected_document": "owner-only.pdf",
                        "expected_passage": "SILVERMAPLE",
                        "language": "en",
                        "persona": "owner",
                    },
                    {
                        "id": "other-hidden",
                        "question": "گودام کا کوڈ کیا ہے؟",
                        "expected_document": "owner-only.pdf",
                        "expected_passage": "SILVERMAPLE",
                        "language": "ur",
                        "persona": "outsider",
                        "expect_abstain": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "personas" / "cli.json").write_text(
        json.dumps({"owner": USER_EMAIL, "outsider": USER2_EMAIL}),
        encoding="utf-8",
    )
    from app.rag.evaluation.__main__ import main

    assert main(["--dataset", "cli.json", "--personas", "cli.json", "--k", "1", "--k", "3"]) == 0
    output = capsys.readouterr().out
    assert "Citation precision  1.0000" in output
    assert "Abstention accuracy 1.0000" in output
    assert "Wrote " in output
    report = next((root / "reports").glob("cli-*.json"))
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["baseline"]["vector_index"] == "faiss"
    assert body["cases"][1]["language"] == "ur"
    assert "گودام" in body["cases"][1]["question"]
    assert report.resolve().is_relative_to(root.resolve())
    outside = root.parent.parent / "outside.json"
    assert main(["--dataset", str(outside), "--personas", "cli.json"]) == 2


def _acl_dataset() -> str:
    return json.dumps(
        {
            "name": "acl",
            "cases": [
                {
                    "id": "private-owner",
                    "question": "What is the SILVERMAPLE warehouse door code?",
                    "expected_document": "owner-only.pdf",
                    "expected_passage": "SILVERMAPLE",
                    "language": "en",
                    "persona": "private_owner",
                },
                {
                    "id": "private-admin",
                    "question": "What is the SILVERMAPLE warehouse door code?",
                    "expected_document": "owner-only.pdf",
                    "expected_passage": "SILVERMAPLE",
                    "language": "en",
                    "persona": "admin",
                    "expect_abstain": True,
                },
                {
                    "id": "restricted-member",
                    "question": "billing refund window kitne days hain?",
                    "expected_document": "refunds.pdf",
                    "expected_passage": "thirty days",
                    "language": "roman_ur",
                    "persona": "member",
                },
                {
                    "id": "restricted-outsider",
                    "question": "billing refund window kitne days hain?",
                    "expected_document": "refunds.pdf",
                    "expected_passage": "thirty days",
                    "language": "roman_ur",
                    "persona": "outsider",
                    "expect_abstain": True,
                },
            ],
        }
    )


def _sample_report() -> dict:
    return {
        "schema_version": 1,
        "baseline": {"vector_index": "faiss", "embedding_model": "test/fake-bow-embedder", "embedding_dimension": 384},
        "dataset": {"name": "sample", "sha256": "abc123", "cases": 1, "description": ""},
        "parameters": {"k": [1, 3], "min_similarity": 0.15, "max_context_chars": 12000, "top_k": 3},
        "metrics": {
            "recall@1": 1.0,
            "recall@3": 1.0,
            "ndcg@1": 1.0,
            "ndcg@3": 1.0,
            "mrr": 1.0,
            "citation_precision": 1.0,
            "abstention_accuracy": None,
            "answerable_cases": 1,
            "abstention_cases": 0,
            "latency_ms": {"mean": 1.0, "p50": 1.0, "p95": 1.0},
        },
        "by_language": {
            "en": {"cases": 1, "recall": 1.0, "mrr": 1.0, "citation_precision": 1.0, "abstention_accuracy": None}
        },
        "cases": [
            {
                "id": "refund-en",
                "question": "How long is the refund window?",
                "language": "en",
                "persona": "owner",
                "expected_document": "policy.pdf",
                "expected_passage": "30 days",
                "expect_abstain": False,
                "abstained": False,
                "abstention_correct": None,
                "gold_chunks": 1,
                "retrieved": 1,
                "first_relevant_rank": 1,
                "recall": {"1": 1.0, "3": 1.0},
                "mrr": 1.0,
                "ndcg": {"1": 1.0, "3": 1.0},
                "citation_precision": 1.0,
                "latency_ms": 1.0,
            }
        ],
    }
