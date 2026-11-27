"""Opt-in local-model answer measurements on synthetic data and a disposable database.

KNOWLEDGE_REAL_MODEL=qwen2.5:3b and PROVIDER_TRUSTED_PRIVATE_ORIGINS=http://127.0.0.1:11434
must be set before running through scripts/irfanix_postgres_tests.py. Never contacts a cloud
provider, reads live credentials, edits the fixed corpus, or executes generated code.
"""
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import ADMIN_EMAIL, login
from tests.rag_helpers import make_pdf, ready

MODEL = os.environ.get("KNOWLEDGE_REAL_MODEL", "")
pytestmark = pytest.mark.skipif(not MODEL, reason="Opt-in local model measurement")
ROOT = Path(__file__).resolve().parents[1]


def test_real_local_model_knowledge_core(client):
    login(client, ADMIN_EMAIL)
    created = client.post("/api/admin/providers/custom", json={
        "display_name": "Synthetic local acceptance", "base_url": "http://127.0.0.1:11434/v1",
        "model": MODEL, "api_key": "local-test-placeholder-key",
    })
    assert created.status_code == 200, created.text
    provider = created.json()
    route = f"/api/admin/providers/{provider['provider']}"
    configured = client.put(route, json={
        "model": MODEL, "settings": {"max_tokens": 256, "temperature": 0},
        "expected_version": provider["version"],
    })
    assert configured.status_code == 200, configured.text
    activated = client.post(f"{route}/activate", json={"expected_version": configured.json()["version"]})
    assert activated.status_code == 200, activated.text
    ready(client, "knowledge-core-refund.pdf", make_pdf(
        "Refund policy: customers may request a refund within 30 days of purchase by contacting billing support."
    ), visibility="shared")
    ready(client, "knowledge-core-maintenance.pdf", make_pdf(
        "Server maintenance: cooling units are inspected every quarter by the facilities team."
    ), visibility="shared")
    corpus = ROOT / "evals/knowledge_core.json"
    dataset = json.loads(corpus.read_text(encoding="utf-8"))
    report = {
        "measured_at": datetime.now(UTC).isoformat(), "model": MODEL,
        "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
        "scope": "real local model via authenticated conversation API; synthetic documents; deterministic test embedder; keyword checks need human review",
        "cases": [],
    }
    chats = {}
    output = ROOT / "evals/reports" / ("knowledge-core-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for case in dataset["cases"]:
            thread = case.get("thread", case["id"])
            if thread not in chats:
                chat = client.post("/api/conversations", json={})
                assert chat.status_code == 201, chat.text
                chats[thread] = chat.json()["id"]
            started = time.perf_counter()
            result = client.post(f"/api/conversations/{chats[thread]}/messages", json={"question": case["question"]})
            measured = {"id": case["id"], "category": case["category"], "http_status": result.status_code,
                        "latency_seconds": round(time.perf_counter() - started, 3), "passed": False}
            if result.status_code == 200:
                answer = result.json()["assistant_message"]["response"]
                checks = {
                    "answer_terms": all(re.search(pattern, answer["answer"], re.I) for pattern in case["must_match"]),
                    "not_refused": not answer["insufficient_evidence"],
                    "expected_mode": answer["general_knowledge"] == case["general"],
                    "sources": (not answer["citations"]) if case["general"] else (
                        bool(answer["citations"]) and all(c["filename"] == case["source"] for c in answer["citations"])
                    ),
                }
                measured.update(answer=answer["answer"], strategy=answer["strategy"], checks=checks, passed=all(checks.values()))
            else:
                measured["error_code"] = result.json().get("error", {}).get("code", "unknown")
            report["cases"].append(measured)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        report["measured"] = len(report["cases"])
        report["passed"] = sum(case["passed"] for case in report["cases"])
        report["total"] = len(dataset["cases"])
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert report["passed"] == report["total"], f"Real-model gaps recorded in {output.relative_to(ROOT)}: {report['passed']}/{report['total']} passed"
