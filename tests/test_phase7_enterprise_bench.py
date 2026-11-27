"""Opt-in synthetic Phase 7 benchmarks. Run with RUN_PHASE7_BENCHMARKS=1.

These use the existing PostgreSQL test database, FAISS store, API and test embedder. API load stubs only
answer generation and does not call a provider. The benchmarks are excluded from normal test runs because
the API load pass issues 400 requests.
"""

from __future__ import annotations

import json
import os
import threading
import time
import asyncio
from pathlib import Path

import httpx
import numpy as np
import psutil
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import SESSION_COOKIE_NAME
from app.db.models import User
from app.main import app
from app.rag.query_expansion import retrieval_queries
from app.rag.retrieval import retrieve
from app.schemas.rag import Citation, RagChatResponse
from tests.conftest import ADMIN_EMAIL, PASSWORD, USER_EMAIL, _PASSWORD_HASH, login, run_db
from tests.rag_helpers import process_all, upload
from tests.test_access_control import new_group, set_access, user_id
from tests.test_document_formats import make_xlsx

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PHASE7_BENCHMARKS") != "1",
    reason="Set RUN_PHASE7_BENCHMARKS=1 to run synthetic Phase 7 load benchmarks.",
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "data" / "p7-enterprise" / "reports" / "validation.json"
USERS = [f"phase7-load-{i:02d}@example.com" for i in range(20)]
REFUND_EN = "What is the refund deadline for invoice disputes?"
REFUND_UR = "انوائس تنازعہ کے لیے رقم واپسی کتنے دن کی ہے؟"
REFUND_ROMAN = "invoice disputes ke liye refund kitne din ka hai?"
GROUP_SECRET = "BLUEHERON"
PRIVATE_SECRET = "PRIVATE-P7-ALPHA"


def _save_section(name: str, section: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(REPORT_PATH.read_text(encoding="utf-8")) if REPORT_PATH.exists() else {}
    existing[name] = section
    REPORT_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _metrics(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "p50_ms": round(float(np.percentile(samples, 50)), 2),
        "p95_ms": round(float(np.percentile(samples, 95)), 2),
        "p99_ms": round(float(np.percentile(samples, 99)), 2),
        "max_ms": round(max(samples), 2),
    }


async def _api_request(client, token: str, question: str) -> tuple[float, int, dict]:
    started = time.perf_counter()
    response = await client.post(
        "/api/rag/chat",
        json={"question": question},
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}", "X-Requested-With": "test"},
    )
    return (time.perf_counter() - started) * 1000, response.status_code, response.json()


def _retrieved_documents(client, email: str, question: str) -> set[str]:
    return client.portal.call(_retrieved_documents_async, email, question)


async def _retrieved_documents_async(email: str, question: str) -> set[str]:
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.email == email))
        chunks = await retrieve(
            db,
            user,
            app.state.rag.embedder,
            app.state.rag.vector_store,
            question,
            top_k=3,
            min_similarity=-1.0,
            max_context_chars=12_000,
            query_transform=retrieval_queries,
        )
        return {str(chunk.document_id) for chunk in chunks}


def _run_wave(
    client, tokens: list[str], questions: tuple[str, ...], concurrency: int, count: int
) -> tuple[list, float, float, int, int, int]:
    process = psutil.Process(os.getpid())
    stop = threading.Event()
    peak_rss = [process.memory_info().rss]
    minimum_free = [psutil.virtual_memory().available]

    def monitor() -> None:
        while not stop.wait(0.05):
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
            minimum_free[0] = min(minimum_free[0], psutil.virtual_memory().available)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    cpu_before = sum(process.cpu_times()[:2])
    started = time.perf_counter()
    try:
        async def send_wave():
            transport = httpx.ASGITransport(app=app)
            results = []
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                for start in range(0, count, concurrency):
                    end = min(start + concurrency, count)
                    results.extend(
                        await asyncio.gather(
                            *(
                                _api_request(http, tokens[i % len(tokens)], questions[i % len(questions)])
                                for i in range(start, end)
                            )
                        )
                    )
            return results

        # Run ASGI directly on the app's existing loop, retaining real routing, middleware and DB sessions.
        results = client.portal.call(send_wave)
    finally:
        stop.set()
        monitor_thread.join()
    wall_seconds = time.perf_counter() - started
    cpu_seconds = sum(process.cpu_times()[:2]) - cpu_before
    peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
    statuses = [status for _, status, _ in results]
    errors = sum(status >= 400 for status in statuses)
    return results, wall_seconds, cpu_seconds, errors, peak_rss[0], minimum_free[0]


def test_phase7_api_concurrency_and_acl(client, monkeypatch):
    """Exercise multilingual API requests, private/group ACLs and revocation at 1/5/10/20 users."""
    from app.api.routes import rag as rag_route
    from app.core.security import SESSION_COOKIE_NAME

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_min_similarity", -1.0)

    # Measure authentication + HTTP routing + production retrieval without provider latency, answer
    # synthesis, or answer checking. This keeps the API load figures focused on the route/retrieval path.
    async def benchmark_respond(
        db, user, rag, app_settings, get_llm, question, *, history, document_ids,
        focus_document_ids, memory=None, conversation_id=None, previous_charts=None,
    ):
        chunks = await retrieve(
            db, user, rag.embedder, rag.vector_store, question,
            top_k=app_settings.rag_top_k,
            min_similarity=app_settings.rag_min_similarity,
            max_context_chars=app_settings.rag_max_context_chars,
            document_ids=document_ids,
            query_transform=retrieval_queries,
        )
        return RagChatResponse(
            answer="", insufficient_evidence=True,
            citations=[
                Citation(
                    source_id=f"S{index}", document_id=chunk.document_id, filename=chunk.filename,
                    page_number=chunk.page_number, locator=chunk.location, location=chunk.location_label,
                    chunk_index=chunk.chunk_index, score=round(chunk.score, 4), snippet=chunk.content[:320],
                )
                for index, chunk in enumerate(chunks, 1)
            ],
            retrieved_chunks=len(chunks), strategy="semantic" if chunks else "none", model=None, provider=None,
        )

    monkeypatch.setattr(rag_route, "rag_respond", benchmark_respond)
    login(client, ADMIN_EMAIL)
    admin_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert admin_token

    run_db(
        lambda connection: connection.execute(
            User.__table__.insert(),
            [{"email": email, "role": "user", "password_hash": _PASSWORD_HASH} for email in USERS],
        )
    )
    tokens = []
    for email in USERS:
        client.cookies.clear()
        response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 200, response.text
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token
        tokens.append(token)

    # Restore the admin session for synthetic shared and group-restricted documents.
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, admin_token)
    shared = upload(
        client,
        "phase7-refund.txt",
        b"Invoice disputes are eligible for a refund within thirty days.",
        visibility="shared",
        content_type="text/plain",
    )
    assert shared.status_code == 202, shared.text
    shared_id = shared.json()["id"]

    group = new_group(client, "Phase 7 synthetic billing group")
    restricted = upload(
        client,
        "phase7-restricted.txt",
        f"Restricted project key: {GROUP_SECRET}.".encode(),
        content_type="text/plain",
    )
    assert restricted.status_code == 202, restricted.text
    restricted_id = restricted.json()["id"]
    assert set_access(client, restricted_id, "restricted", groups=[group]).status_code == 200
    for email in USERS[:5]:
        added = client.post(f"/api/admin/access/groups/{group}/members", json={"email": email})
        assert added.status_code == 204, added.text

    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, tokens[0])
    private = upload(
        client,
        "phase7-private.txt",
        f"Private account key: {PRIVATE_SECRET}.".encode(),
        content_type="text/plain",
    )
    assert private.status_code == 202, private.text
    private_id = private.json()["id"]
    client.cookies.clear()
    assert process_all(client) == 3

    # Positive and negative ACL controls before revocation.
    member_docs = _retrieved_documents(client, USERS[0], "What is the restricted project key?")
    outsider_docs = _retrieved_documents(client, USERS[19], "What is the restricted project key?")
    owner_docs = _retrieved_documents(client, USERS[0], "What is the private account key?")
    outsider_private_docs = _retrieved_documents(client, USERS[19], "What is the private account key?")
    assert restricted_id in member_docs
    assert restricted_id not in outsider_docs
    assert private_id in owner_docs
    assert private_id not in outsider_private_docs

    # Each wave makes 100 API calls with separate authenticated sessions and a balanced language mix.
    questions = (REFUND_EN, REFUND_UR, REFUND_ROMAN)
    load_results = {}
    for concurrency in (1, 5, 10, 20):
        samples, wall, cpu, errors, peak_rss, min_free = _run_wave(
            client, tokens, questions, concurrency, count=100
        )
        latencies = [item[0] for item in samples]
        load_results[str(concurrency)] = {
            "requests": len(samples),
            "concurrency": concurrency,
            "api_latency": _metrics(latencies),
            "throughput_requests_per_second": round(len(samples) / wall, 2),
            "wall_seconds": round(wall, 3),
            "process_cpu_seconds": round(cpu, 3),
            "process_peak_rss_mb": round(peak_rss / 2**20, 1),
            "system_free_ram_min_gb": round(min_free / 2**30, 2),
            "http_errors": errors,
            "query_language_mix": {"en": 34, "ur": 33, "roman_ur": 33},
            "retrieved_chunk_range": [
                min(result.get("retrieved_chunks", 0) for _, _, result in samples),
                max(result.get("retrieved_chunks", 0) for _, _, result in samples),
            ],
        }
        assert errors == 0, load_results[str(concurrency)]

    # Revoke group membership while the same 20 sessions remain active. The next concurrent wave must not
    # return the restricted source or expose its contents in any fake-provider request.
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, admin_token)
    for email in USERS[:5]:
        removed = client.delete(
            f"/api/admin/access/groups/{group}/members/{user_id(email)}"
        )
        assert removed.status_code == 204, removed.text
    revoked_samples, wall, cpu, errors, peak_rss, min_free = _run_wave(
        client, tokens, ("What is the restricted project key?",), 20, count=20
    )
    revoked_responses = [body for _, status, body in revoked_samples if status == 200]
    assert errors == 0
    assert all(restricted_id not in {c["document_id"] for c in body["citations"]} for body in revoked_responses)

    async def verify_revoked():
        return await asyncio.gather(
            *(_retrieved_documents_async(email, "What is the restricted project key?") for email in USERS)
        )

    leaked = [restricted_id in documents for documents in client.portal.call(verify_revoked)]
    assert not any(leaked)

    _save_section(
        "api_concurrency",
        {
            "database": "isolated *_test database",
            "corpus": "three synthetic documents; shared, group-restricted and owner-private",
            "provider": "not called by the API load stub",
            "embedding": "existing test fake embedder; query expansion is the production Phase 6 helper",
            "answer_generation": "stubbed; latency includes authenticated HTTP route and production permission-filtered retrieval, not provider or answer synthesis",
            "requests_per_wave": 100,
            "waves": load_results,
            "acl": {
                "group_member_reads_restricted": True,
                "outsider_blocked_before_revocation": True,
                "private_owner_reads_private": True,
                "outsider_blocked_from_private": True,
                "concurrent_next_wave_after_revocation": {
                    "requests": len(revoked_samples),
                    "errors": errors,
                    "restricted_source_returned": False,
                    "secret_in_provider_context": "not applicable; answer generation was stubbed",
                    "restricted_document_in_20_concurrent_direct_retrievals": sum(leaked),
                    "latency": _metrics([item[0] for item in revoked_samples]),
                    "throughput_requests_per_second": round(len(revoked_samples) / wall, 2),
                    "process_cpu_seconds": round(cpu, 3),
                    "process_peak_rss_mb": round(peak_rss / 2**20, 1),
                    "system_free_ram_min_gb": round(min_free / 2**30, 2),
                },
                "tenant_isolation": "not testable: no tenant entity/scope in current schema",
            },
        },
    )


def test_phase7_xlsx_worker_throughput(client):
    """Measure a bounded synthetic XLSX-only worker batch; PDF OCR stress is reused from Phase 4 reports."""
    login(client, USER_EMAIL)
    payloads = [make_xlsx(rows=1000 + i) for i in range(10)]
    documents = []
    for index, payload in enumerate(payloads):
        response = upload(
            client,
            f"phase7-workbook-{index:02d}.xlsx",
            payload,
            content_type="application/octet-stream",
        )
        assert response.status_code == 202, response.text
        documents.append(response.json()["id"])

    process = psutil.Process(os.getpid())
    peak_rss = [process.memory_info().rss]
    minimum_free = [psutil.virtual_memory().available]
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.02):
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
            minimum_free[0] = min(minimum_free[0], psutil.virtual_memory().available)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    cpu_before = sum(process.cpu_times()[:2])
    started = time.perf_counter()
    try:
        processed = process_all(client)
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        monitor_thread.join()
    worker_cpu_seconds = sum(process.cpu_times()[:2]) - cpu_before
    peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
    chunks = run_db(
        lambda connection: connection.scalar(
            text("SELECT COALESCE(SUM(chunk_count), 0) FROM documents WHERE id::text = ANY(:ids)"),
            {"ids": documents},
        )
    )
    result = {
        "format": "xlsx",
        "synthetic_documents": len(documents),
        "rows_per_document": "1000-1009 inventory rows plus budget sheet",
        "processed": processed,
        "worker_wall_seconds": round(elapsed, 3),
        "documents_per_second": round(processed / elapsed, 3) if elapsed else None,
        "chunks_indexed": int(chunks),
        "chunks_per_second": round(chunks / elapsed, 2) if elapsed else None,
        "average_worker_seconds_per_document": round(elapsed / processed, 3) if processed else None,
        "worker_process_cpu_seconds": round(worker_cpu_seconds, 3),
        "process_peak_rss_mb": round(peak_rss[0] / 2**20, 1),
        "system_free_ram_min_gb": round(minimum_free[0] / 2**30, 2),
        "environment": "isolated *_test database, built-in XLSX parser, fake embedder, local FAISS",
        "ocr": "not applicable; XLSX benchmark only",
    }
    _save_section("xlsx_worker_throughput", result)
    assert processed == len(documents)
