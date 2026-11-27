"""The evaluation gate's own logic: thresholds, the database guard, the golden set and its corpus.

The full gate (corpus upload, real embeddings, optional LLM) runs as `python -m app.rag.evaluation.gate`
and in CI; these tests keep its inputs and decisions honest without that cost.
"""

import json

import pytest
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.rag.evaluation import gate, golden
from app.rag.evaluation.dataset import EvaluationError, load_dataset


def test_min_and_max_rules_and_a_missing_metric_fail_closed():
    checks = gate._checks(
        {"recall@5": 0.96, "permission_leaks": 0, "mrr": None},
        {"recall@5": {"min": 0.97}, "permission_leaks": {"max": 0}, "mrr": {"min": 0.9}, "ndcg@5": {"min": 0.9}},
    )
    assert [(c.metric, c.passed) for c in checks] == [
        ("recall@5", False),
        ("permission_leaks", True),
        ("mrr", False),
        ("ndcg@5", False),
    ]


def test_thresholds_need_exactly_one_bound(tmp_path):
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"retrieval": {"mrr": {"min": 0.9, "max": 1.0}}}), encoding="utf-8")
    with pytest.raises(gate.GateError, match="exactly one"):
        gate.load_thresholds(path)
    assert gate.load_thresholds(gate.THRESHOLDS)["retrieval"]["permission_leaks"] == {"max": 0}


def test_the_gate_never_rebuilds_a_database_that_is_not_an_evalgate_one(monkeypatch):
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgresql+asyncpg://app:secret@localhost/infraspine_ai")
    with pytest.raises(gate.GateError, match="_evalgate"):
        gate.gate_database_url(get_settings())
    monkeypatch.delenv("EVAL_DATABASE_URL")
    source = make_url(get_settings().database_url.get_secret_value())
    result = make_url(gate.gate_database_url(get_settings()))
    assert result.database == f"{source.database}{gate._SUFFIX}"


def test_golden_cases_match_the_corpus_and_personas():
    dataset = load_dataset(gate.GOLDEN)
    files = golden.corpus()
    assert len(dataset.cases) >= 60
    assert {case.persona for case in dataset.cases} <= set(golden.PERSONAS)
    for case in dataset.cases:
        if case.expected_document:
            assert case.expected_document in files, case.id
    protected = set(golden._PRIVATE) | golden._RESTRICTED
    assert {case.expected_document for case in dataset.cases if case.category == "unauthorized"} <= protected
    assert {"en", "ur", "roman_ur"} <= {case.language for case in dataset.cases}
    assert any(case.history and case.question == "?" for case in dataset.cases)
    assert any(case.expected_document == "it-memo.docx" and len(case.expected_facts) > 1 for case in dataset.cases)


def test_history_turns_are_validated(tmp_path):
    def write(history):
        path = tmp_path / "d.json"
        case = {
            "id": "c", "question": "?", "language": "en", "persona": "owner", "category": "answerable",
            "expected_document": "a.txt", "expected_facts": ["x"], "history": history,
        }
        path.write_text(json.dumps({"name": "d", "cases": [case]}), encoding="utf-8")
        return path

    loaded = load_dataset(write([{"question": "What is x?", "answer": None}, {"question": "And y?", "answer": "y [S1]"}]))
    assert loaded.cases[0].history == (("What is x?", None), ("And y?", "y [S1]"))
    with pytest.raises(EvaluationError, match="history"):
        load_dataset(write([{"answer": "no question"}]))
    with pytest.raises(EvaluationError, match="history"):
        load_dataset(write([{"question": "q"}] * 6))


def test_paced_client_waits_out_a_rate_limit_and_keeps_under_the_quota(monkeypatch):
    import asyncio

    from app.core.errors import LLMRateLimitError

    waits: list[float] = []
    real_sleep = asyncio.sleep  # gate.asyncio is the asyncio module: patching it patches asyncio.sleep

    async def no_sleep(seconds):
        waits.append(seconds)

    class Flaky:
        calls = 0

        async def chat(self, message, *, system_prompt=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise LLMRateLimitError()
            return message

    monkeypatch.setattr(gate.asyncio, "sleep", no_sleep)
    paced = gate.Paced(Flaky(), rpm=0)
    assert asyncio.run(paced.chat("ok")) == "ok" and Flaky.calls == 2 and waits == [65]

    class Echo:
        async def chat(self, message, *, system_prompt=None):
            return message

    monkeypatch.setattr(gate.asyncio, "sleep", real_sleep)  # real waiting: the quota must not be exceeded

    async def three():
        limited = gate.Paced(Echo(), rpm=2)
        limited._sent = [asyncio.get_running_loop().time() - 59.95] * 2  # quota full, frees up in ~50 ms
        return await limited.chat("third")

    assert asyncio.run(three()) == "third"
