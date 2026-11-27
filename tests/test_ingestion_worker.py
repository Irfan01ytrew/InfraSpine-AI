"""Isolated background document processing: separate worker processes, parser child processes with time and
memory limits, leases and crash recovery, cancellation, graceful shutdown, index ownership and status.

Real PostgreSQL, real FAISS and real parser child processes; fake embedder and fake LLM. Misbehaving parsers
come from `tests/isolation_targets.py` (child processes can only run importable module-level functions).
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.db.models import DocumentChunk
from app.main import app
from app.rag import components
from app.rag.ingestion import IngestionWorker
from app.rag.isolation import run_isolated
from app.rag.parsers.base import ParseError
from app.rag.vector_store import IndexOwnerLock
from app.worker import main as worker_main
from tests import isolation_targets
from tests.conftest import ADMIN_EMAIL, RAG_TEST_DIR, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, ask, document, make_pdf, upload, vector_ids
from tests.test_document_formats import make_csv, make_docx, make_pptx, make_xlsx

TXT = b"Backup runbook: verify the nightly backup job completed before 06:00.\nEscalate failures to the storage team."


@pytest.fixture
def fast_leases(monkeypatch):
    """Short lease renewal interval so cancellation and renewal are observable within a test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_worker_poll_seconds", 0.2)
    monkeypatch.setattr(settings, "rag_processing_lease_seconds", 10)
    return settings


def worker(*, isolate: bool = False, target=None, separate_process: bool = False) -> IngestionWorker:
    """A worker sharing the app's database. `separate_process=True` mimics `python -m app.worker`."""
    if separate_process:
        built = components.build_worker(get_settings(), app.state.sessionmaker)
        built.isolate = isolate
    else:
        rag = app.state.rag
        built = IngestionWorker(app.state.sessionmaker, get_settings(), rag.storage, rag.embedder, rag.indexer, isolate=isolate)
    if target is not None:
        built.target = target
    return built


def drain(client, w: IngestionWorker) -> int:
    count = 0
    while client.portal.call(w.process_next):
        count += 1
    return count


def doc_row(document_id: str):
    return run_db(
        lambda c: c.execute(
            text("SELECT status, attempts, error_code, error_message, locked_until, version FROM documents WHERE id = :d"),
            {"d": document_id},
        )
    ).one()


def chunk_ids(document_id: str | None = None) -> set[int]:
    query = "SELECT id FROM document_chunks" + (" WHERE document_id = :d" if document_id else "")
    return {r[0] for r in run_db(lambda c: c.execute(text(query), {"d": document_id})).all()}


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


def stored_path(document_id: str):
    key = run_db(lambda c: c.scalar(text("SELECT storage_key FROM documents WHERE id = :d"), {"d": document_id}))
    return RAG_TEST_DIR / "uploads" / key


def child_pid(document_id: str) -> int:
    pid_file = stored_path(document_id).with_name(stored_path(document_id).name + ".pid")
    return int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ successful ingestion


def test_isolated_parsing_preserves_every_format_and_locator(client):
    login(client, USER_EMAIL)
    files = {
        "a.pdf": make_pdf("Cooling units are inspected every quarter."),
        "b.docx": make_docx(),
        "c.xlsx": make_xlsx(),
        "d.pptx": make_pptx(),
        "e.txt": TXT,
        "f.csv": make_csv(),
    }
    ids = {name: upload(client, name, data, content_type="application/octet-stream").json()["id"] for name, data in files.items()}
    assert drain(client, worker(isolate=True)) == 6

    kinds = {"a.pdf": "page", "b.docx": "section", "c.xlsx": "sheet", "d.pptx": "slide", "e.txt": "lines", "f.csv": "rows"}
    for name, document_id in ids.items():
        assert document(client, document_id)["status"] == "ready", name
        locators = run_db(lambda c: c.execute(text("SELECT locator FROM document_chunks WHERE document_id = :d"), {"d": document_id})).scalars().all()
        assert {loc["kind"] for loc in locators} == {kinds[name]}, name
    assert document(client, ids["a.pdf"])["page_count"] == 1
    assert vector_ids() == chunk_ids()  # the embedded worker applied every result to the index


def test_separate_worker_writes_postgres_only_and_the_api_index_follows(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = upload(client, "assets.xlsx", make_xlsx(), content_type="application/octet-stream").json()
    before = vector_ids()

    assert drain(client, worker(separate_process=True, isolate=True)) == 1
    assert document(client, doc["id"])["status"] == "ready"
    assert chunk_ids(doc["id"]) and vector_ids() == before  # the worker never touched the index

    assert client.portal.call(app.state.rag.indexer.sync_changes) is True
    assert vector_ids() == chunk_ids()
    assert client.portal.call(app.state.rag.indexer.sync_changes) is False  # nothing new: cheap no-op

    fake_provider.rag_reply = lambda message: "There are 40 [S1]."
    citation = ask(client, "How many UPS battery units are in Hall B?").json()["citations"][0]
    assert citation["location"] == "Sheet 'Inventory' A2:C3"


# ------------------------------------------------------------------ claims, leases and recovery


def test_concurrent_workers_never_claim_the_same_job(client):
    login(client, USER_EMAIL)
    ids = {upload(client, f"{i}.txt", TXT).json()["id"] for i in range(3)}
    workers = [worker(), worker(separate_process=True)]

    async def race():
        return await asyncio.gather(*(w._claim() for w in workers * 3))

    jobs = [j for j in client.portal.call(race) if j is not None]
    assert sorted(str(j.document_id) for j in jobs) == sorted(ids)  # each claimed exactly once
    assert {doc_row(i).status for i in ids} == {"processing"}


def test_abandoned_jobs_are_recovered_after_the_lease_expires(client):
    login(client, USER_EMAIL)
    doc = upload(client, "a.txt", TXT).json()
    crashed = worker(separate_process=True)
    assert client.portal.call(crashed._claim) is not None  # the worker "dies" while holding the job
    assert client.portal.call(worker().process_next) is False  # leased: nobody else takes it yet

    run_db(lambda c: c.execute(text("UPDATE documents SET locked_until = now() - interval '1 second'")))
    assert client.portal.call(worker().process_next) is True
    row = doc_row(doc["id"])
    assert (row.status, row.attempts) == ("ready", 2)

    # A job abandoned on every attempt stops with an actionable reason instead of looping forever.
    other = upload(client, "b.txt", TXT).json()
    run_db(lambda c: c.execute(
        text("UPDATE documents SET status = 'processing', attempts = 3, locked_until = now() - interval '1 second' WHERE id = :d"),
        {"d": other["id"]},
    ))
    client.portal.call(worker().process_next)
    row = doc_row(other["id"])
    assert (row.status, row.error_code) == ("failed", "processing_abandoned")
    assert "Reprocess" in row.error_message


def test_leases_are_renewed_while_a_document_is_processed(client, fast_leases, monkeypatch):
    monkeypatch.setenv("TEST_PARSER_SLEEP", "3")
    login(client, USER_EMAIL)
    doc = upload(client, "slow.txt", TXT).json()
    future = client.portal.start_task_soon(worker(isolate=True, target=isolation_targets.sleep_then_parse).process_next)
    child_pid(doc["id"])
    first = doc_row(doc["id"]).locked_until
    time.sleep(1.0)
    assert doc_row(doc["id"]).locked_until > first  # renewed, so it is never mistaken for an abandoned job
    assert future.result(timeout=60) is True
    assert doc_row(doc["id"]).status == "ready"


# ------------------------------------------------------------------ limits and failures


def test_timeouts_kill_the_parser_and_fail_with_an_actionable_reason(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_processing_timeout_seconds", 5)
    login(client, USER_EMAIL)
    doc = upload(client, "stuck.txt", TXT).json()
    assert client.portal.call(worker(isolate=True, target=isolation_targets.hang).process_next) is True
    row = doc_row(doc["id"])
    assert (row.status, row.error_code) == ("failed", "processing_timeout")
    assert "RAG_PROCESSING_TIMEOUT_SECONDS" in row.error_message
    assert not pid_alive(child_pid(doc["id"]))


def test_run_isolated_always_leaves_no_child_behind(tmp_path):
    started = []
    with pytest.raises(ParseError) as caught:
        run_isolated(isolation_targets.hang, (str(tmp_path / "x"),), timeout_seconds=2, memory_bytes=0, on_start=started.append)
    assert caught.value.code == "processing_timeout" and not started[0].is_alive()
    assert run_isolated(isolation_targets.add, (2, 3), timeout_seconds=60, memory_bytes=512 * 1024 * 1024) == 5


def test_memory_limit_stops_the_parser(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_parser_memory_mb", 256)
    login(client, USER_EMAIL)
    doc = upload(client, "huge.txt", TXT).json()
    client.portal.call(worker(isolate=True, target=isolation_targets.allocate).process_next)
    row = doc_row(doc["id"])
    assert (row.status, row.error_code) == ("failed", "memory_limit")
    assert "RAG_PARSER_MEMORY_MB" in row.error_message


def test_crashes_are_retried_then_fail(client):
    login(client, USER_EMAIL)
    doc = upload(client, "crash.txt", TXT).json()
    crashing = worker(isolate=True, target=isolation_targets.crash)
    client.portal.call(crashing.process_next)
    row = doc_row(doc["id"])
    assert (row.status, row.error_code) == ("pending", "processing_crashed")
    assert "Retrying (attempt 1 of 3)" in row.error_message
    assert drain(client, crashing) == 2  # RAG_RETRY_BASE_SECONDS=0 in tests
    row = doc_row(doc["id"])
    assert (row.status, row.error_code, row.attempts) == ("failed", "processing_crashed", 3)


def test_native_docling_exit_isolated_and_builtin_fallback_must_extract_content(client):
    """A parser access violation cannot kill the worker or make an empty fallback look successful."""
    login(client, USER_EMAIL)
    doc = upload(client, "native-crash.txt", TXT).json()
    isolated = worker(isolate=True, target=isolation_targets.docling_native_exit_then_fallback)

    assert client.portal.call(isolated.process_next) is True
    row = doc_row(doc["id"])
    assert (row.status, row.error_code) == ("pending", "processing_crashed")
    assert client.get("/health").status_code == 200  # the API remains alive after the child exits abnormally

    assert drain(client, isolated) == 1
    row = doc_row(doc["id"])
    assert row.status == "ready"
    async def read_stored_text(connection):
        result = await connection.execute(
            text("SELECT content FROM document_chunks WHERE document_id = :document_id"),
            {"document_id": doc["id"]},
        )
        return result.scalars().all()

    stored_text = run_db(read_stored_text)
    assert any("Backup runbook" in chunk for chunk in stored_text)

    empty = upload(client, "native-crash-empty.txt", TXT).json()
    no_content = worker(isolate=True, target=isolation_targets.docling_native_exit_then_empty)
    assert client.portal.call(no_content.process_next) is True
    assert doc_row(empty["id"]).error_code == "processing_crashed"
    assert drain(client, no_content) == 1
    row = doc_row(empty["id"])
    assert (row.status, row.error_code) == ("failed", "no_text")


def test_files_grown_beyond_the_size_limit_are_refused_by_the_worker(client):
    login(client, USER_EMAIL)
    doc = upload(client, "a.txt", TXT).json()
    stored_path(doc["id"]).write_bytes(b"x" * (2 * 1024 * 1024))  # over RAG_MAX_UPLOAD_MB=1
    client.portal.call(worker().process_next)
    assert doc_row(doc["id"]).error_code == "file_too_large"


# ------------------------------------------------------------------ cancellation and shutdown


def test_queued_and_running_jobs_can_be_cancelled(client, fast_leases):
    login(client, USER_EMAIL)
    queued = upload(client, "queued.txt", TXT).json()
    cancelled = client.post(f"/api/rag/documents/{queued['id']}/cancel")
    assert cancelled.status_code == 200
    assert (cancelled.json()["status"], cancelled.json()["error_code"]) == ("failed", "cancelled")
    assert client.post(f"/api/rag/documents/{queued['id']}/cancel").status_code == 409  # nothing to cancel
    assert client.portal.call(worker().process_next) is False
    assert client.post(f"/api/rag/documents/{queued['id']}/reprocess").status_code == 202
    assert drain(client, worker()) == 1 and document(client, queued["id"])["status"] == "ready"

    running = upload(client, "running.txt", TXT).json()
    future = client.portal.start_task_soon(worker(isolate=True, target=isolation_targets.hang).process_next)
    pid = child_pid(running["id"])
    assert client.post(f"/api/rag/documents/{running['id']}/cancel").status_code == 200
    assert future.result(timeout=30) is True  # the worker noticed within a lease interval
    row = doc_row(running["id"])
    assert (row.status, row.error_code) == ("failed", "cancelled") and chunk_ids(running["id"]) == set()
    assert not pid_alive(pid)


def test_replacing_a_file_stops_processing_of_the_old_version(client, fast_leases):
    login(client, USER_EMAIL)
    doc = upload(client, "v1.txt", TXT).json()
    future = client.portal.start_task_soon(worker(isolate=True, target=isolation_targets.hang).process_next)
    pid = child_pid(doc["id"])
    replaced = client.put(f"/api/rag/documents/{doc['id']}/file", files={"file": ("v2.txt", b"Version two text.", "text/plain")})
    assert replaced.status_code == 202
    assert future.result(timeout=30) is True and not pid_alive(pid)
    assert doc_row(doc["id"]).status == "pending"  # the new version waits for its own job
    assert drain(client, worker()) == 1 and document(client, doc["id"])["status"] == "ready"


def test_graceful_shutdown_returns_running_jobs_to_the_queue(client, fast_leases, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_worker_shutdown_grace_seconds", 0)
    login(client, USER_EMAIL)
    doc = upload(client, "a.txt", TXT).json()
    stopping = worker(isolate=True, target=isolation_targets.hang)
    stop = client.portal.call(asyncio.Event)
    future = client.portal.start_task_soon(stopping.run, stop)
    pid = child_pid(doc["id"])
    assert client.get("/api/rag/documents").json()["ingestion"]["workers_online"] == 1

    client.portal.call(stop.set)
    future.result(timeout=60)
    row = doc_row(doc["id"])
    assert (row.status, row.attempts, row.locked_until) == ("pending", 0, None)  # not counted as a failure
    assert not pid_alive(pid)
    assert client.get("/api/rag/documents").json()["ingestion"]["workers_online"] == 0  # unregistered


# ------------------------------------------------------------------ permissions, status and operations


def test_worker_processed_documents_keep_their_permissions(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    private = upload(client, "strategy.txt", b"The Heron budget ceiling is 4.2 million euros.").json()
    drain(client, worker(separate_process=True, isolate=True))
    client.portal.call(app.state.rag.indexer.sync_changes)
    fake_provider.rag_reply = lambda message: "Answer [S1]."

    for email in (USER2_EMAIL, ADMIN_EMAIL):
        login(client, email)
        fake_provider.calls.clear()
        assert client.get(f"/api/rag/documents/{private['id']}").status_code == 404
        assert client.post(f"/api/rag/documents/{private['id']}/cancel").status_code == 404
        body = ask(client, "What is the Heron budget ceiling?").json()
        assert body["retrieved_chunks"] == 0
        assert all("4.2 million" not in message for _, message in fake_provider.calls)
    login(client, USER_EMAIL)
    assert ask(client, "What is the Heron budget ceiling?").json()["citations"][0]["document_id"] == private["id"]


def test_status_reports_workers_and_the_queue(client):
    login(client, ADMIN_EMAIL)
    upload(client, "a.txt", TXT)
    assert client.get("/api/rag/documents").json()["ingestion"] == {"mode": "embedded", "workers_online": 0}

    def heartbeat(age_seconds: int):
        run_db(lambda c: c.execute(text(
            "INSERT INTO ingestion_workers (id, hostname, pid, mode, concurrency, heartbeat_at) "
            "VALUES (:id, 'host', 1, 'worker', 2, now() - make_interval(secs => :age))"
        ), {"id": f"w{age_seconds}", "age": age_seconds}))

    heartbeat(1)
    heartbeat(600)  # stopped long ago
    assert client.get("/api/rag/documents").json()["ingestion"]["workers_online"] == 1
    status = client.get("/api/rag/index").json()
    assert (status["workers_online"], status["documents_queued"], status["documents_processing"]) == (1, 1, 0)


def test_the_vector_index_has_a_single_owner_process(tmp_path):
    lock = IndexOwnerLock(tmp_path)
    lock.acquire()
    try:
        again = IndexOwnerLock(tmp_path)
        again.acquire()  # re-entrant within the owning process
        again.release()
        assert run_isolated(isolation_targets.hold_index_lock, (str(tmp_path),), timeout_seconds=60, memory_bytes=0) == "locked"
    finally:
        lock.release()
    assert run_isolated(isolation_targets.hold_index_lock, (str(tmp_path),), timeout_seconds=60, memory_bytes=0) == "acquired"


def test_worker_command_line(client):
    check = subprocess.run(
        [sys.executable, "-m", "app.worker", "--check"], capture_output=True, text=True, timeout=120, env=os.environ.copy()
    )
    assert check.returncode == 0, check.stderr
    assert "Worker configuration OK" in check.stdout

    login(client, USER_EMAIL)
    doc = upload(client, "a.txt", TXT).json()
    assert worker_main(["--once"]) == 0  # same code path as `python -m app.worker --once`
    assert document(client, doc["id"])["status"] == "ready"
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM ingestion_workers"))) == 0


def test_api_in_worker_mode_never_parses_and_its_index_follows_separate_workers(monkeypatch):
    from fastapi.testclient import TestClient

    from tests.conftest import CSRF

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_ingestion_mode", "worker")
    monkeypatch.setattr(settings, "rag_worker_enabled", True)  # ignored in worker mode: the API must not process jobs
    monkeypatch.setattr(settings, "rag_index_sync_seconds", 0.2)
    with TestClient(app, headers=CSRF) as api:
        login(api, USER_EMAIL)
        doc = upload(api, "a.txt", TXT).json()
        time.sleep(1.5)
        assert document(api, doc["id"])["status"] == "pending"  # queued for a worker process
        assert api.get("/api/rag/documents").json()["ingestion"] == {"mode": "worker", "workers_online": 0}

        assert drain(api, worker(separate_process=True)) == 1
        wait_for(lambda: vector_ids() == chunk_ids() and chunk_ids(doc["id"]))  # applied by the API's sync loop



def test_replica_api_process_fills_its_index_from_the_database_and_writes_no_index_files(monkeypatch):
    """An extra API process (RAG_INDEX_ROLE=replica) searches from memory, follows separate workers through
    PostgreSQL, runs no background workers and never writes the owner's index files."""
    from fastapi.testclient import TestClient

    from app.rag.components import data_dir
    from tests.conftest import CSRF

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_ingestion_mode", "worker")
    monkeypatch.setattr(settings, "rag_index_role", "replica")
    monkeypatch.setattr(settings, "rag_index_sync_seconds", 0.2)
    with TestClient(app, headers=CSRF) as api:
        assert api.get("/health/ready").json()["index_role"] == "replica"
        login(api, USER_EMAIL)
        doc = upload(api, "a.txt", TXT).json()
        assert drain(api, worker(separate_process=True)) == 1
        wait_for(lambda: vector_ids() == chunk_ids() and chunk_ids(doc["id"]))
        assert app.state.rag.index_lock is None
    index_dir = data_dir(settings) / "vector_index"
    assert not any((index_dir / name).exists() for name in ("index.faiss", "manifest.json", "changes.log"))

def test_audio_upload_worker_citation_preview_recovery_and_acl_revocation(client, fake_provider, monkeypatch, tmp_path):
    """Exercise the API, real PyAV decode, worker, timestamp citation, range preview, recovery and live ACL."""
    import io
    import wave
    from types import SimpleNamespace

    import av
    import faster_whisper

    model_path = tmp_path / "synthetic-local-model"
    model_path.mkdir()
    settings = get_settings()
    for name, value in {
        "rag_media_enabled": True,
        "rag_media_model_path": str(model_path),
        "rag_media_max_file_mb": 1,
        "rag_media_device": "cpu",
        "rag_media_cpu_threads": 1,
        "rag_media_max_duration_seconds": 30,
        "rag_media_segment_seconds": 10,
    }.items():
        monkeypatch.setattr(settings, name, value)

    class SyntheticSpeechModel:
        def __init__(self, path, **kwargs):
            assert path == str(model_path)
            assert kwargs["local_files_only"] is True

        def transcribe(self, wav_path, **kwargs):
            assert kwargs["beam_size"] == 1
            with wave.open(wav_path, "rb") as decoded:
                assert decoded.getnframes() > 0  # PyAV decoded the uploaded WAV into a real PCM segment.
            return iter([SimpleNamespace(start=1.25, end=1.9, text="The north pump was replaced on Monday.")]), SimpleNamespace(language="en")

    monkeypatch.setattr(faster_whisper, "WhisperModel", SyntheticSpeechModel)
    assert av.__version__  # the acceptance environment must include the pinned local decoder.

    source = io.BytesIO()
    with wave.open(source, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * (16_000 * 2))
    audio_bytes = source.getvalue()

    activate_provider(client)
    uploaded = upload(client, "pump-maintenance.wav", audio_bytes, visibility="shared", content_type="audio/wav")
    assert uploaded.status_code == 202, uploaded.text
    doc_id = uploaded.json()["id"]
    work = worker(isolate=False)

    # A model-cache outage marks the job failed; after recovery, the normal reprocess action resumes it.
    model_path.rmdir()
    assert client.portal.call(work.process_next) is True
    assert document(client, doc_id)["status"] == "failed"
    model_path.mkdir()
    retried = client.post(f"/api/rag/documents/{doc_id}/reprocess")
    assert retried.status_code == 202, retried.text
    assert client.portal.call(work.process_next) is True
    assert document(client, doc_id)["status"] == "ready"

    login(client, USER_EMAIL)
    fake_provider.rag_reply = lambda _message: "The north pump was replaced on Monday [S1]."
    answer = ask(client, "What happened to the north pump?", [doc_id]).json()
    assert answer["insufficient_evidence"] is False and answer["retrieved_chunks"] > 0
    [citation] = answer["citations"]
    assert citation["document_id"] == doc_id
    assert citation["locator"]["kind"] == "time"
    assert citation["locator"]["start_seconds"] == 1.25
    assert citation["location"] == "00:00:01.250–00:00:01.900"

    preview = f"/api/rag/documents/{doc_id}/media"
    login(client, USER2_EMAIL)
    original = client.get(preview)
    assert original.status_code == 200 and original.content == audio_bytes
    response = client.get(preview, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206
    assert response.headers["content-type"].startswith("audio/")
    assert response.headers["content-range"] == f"bytes 0-15/{len(audio_bytes)}"
    assert response.content == audio_bytes[:16]
    assert client.get(preview, headers={"Range": f"bytes={len(audio_bytes)}-"}).status_code == 416

    # The same preview URL rechecks the live ACL after the document is made private.
    login(client, ADMIN_EMAIL)
    changed = client.put(
        f"/api/rag/documents/{doc_id}/access",
        json={"visibility": "private", "user_emails": [], "group_ids": []},
    )
    assert changed.status_code == 200, changed.text
    login(client, USER2_EMAIL)
    assert client.get(preview).status_code == 404
    refused = ask(client, "What maintenance work happened?", [doc_id]).json()
    assert refused["citations"] == [] and refused["retrieved_chunks"] == 0


@pytest.mark.skipif(
    not os.environ.get("C06_LOCAL_WHISPER_AUDIO"),
    reason="Set C06_LOCAL_WHISPER_AUDIO to run the optional real local-Whisper acceptance.",
)
def test_current_source_local_whisper_audio_upload_worker_and_citation(client, fake_provider, monkeypatch):
    """Exercise a real on-disk Whisper model through the current API and document worker."""
    audio_path = Path(os.environ["C06_LOCAL_WHISPER_AUDIO"]).resolve(strict=True)
    model_path = Path(os.environ["RAG_MEDIA_MODEL_PATH"]).resolve(strict=True)
    settings = get_settings()
    for name, value in {
        "rag_media_enabled": True,
        "rag_media_model_path": str(model_path),
        "rag_media_device": "cpu",
        "rag_media_cpu_threads": 1,
        "rag_media_max_file_mb": 2,
        "rag_media_max_duration_seconds": 60,
    }.items():
        monkeypatch.setattr(settings, name, value)

    audio_bytes = audio_path.read_bytes()
    activate_provider(client)
    uploaded = upload(
        client,
        "c06-local-whisper-synthetic.wav",
        audio_bytes,
        visibility="shared",
        content_type="audio/wav",
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    assert client.portal.call(worker(isolate=False).process_next) is True
    result = document(client, document_id)
    assert result["status"] == "ready" and result["searchable"] and result["chunk_count"] >= 1

    async def read_transcript_chunks():
        async with app.state.sessionmaker() as db:
            return list(
                (
                    await db.execute(
                        select(DocumentChunk.content, DocumentChunk.locator).where(
                            DocumentChunk.document_id == uuid.UUID(document_id)
                        )
                    )
                ).all()
            )

    chunks = client.portal.call(read_transcript_chunks)
    transcript = " ".join(content for content, _locator in chunks)
    assert "ticket" in transcript.lower()
    assert "".join(character for character in transcript if character.isdigit()).find("4821") >= 0
    assert any(locator and locator.get("kind") == "time" for _content, locator in chunks)

    fake_provider.rag_reply = lambda _message: "The maintenance ticket number is 4821 [S1]."
    ambiguous_number = ask(client, "What is the maintenance ticket number?", [document_id]).json()
    assert ambiguous_number["insufficient_evidence"] is True
    assert ambiguous_number["citations"] == [] and "4821" not in ambiguous_number["answer"]

    fake_provider.rag_reply = lambda _message: "The North Pump requires inspection every 30 days [S1]."
    answer = ask(client, "How often does the north pump require inspection?", [document_id])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["insufficient_evidence"] is False and body["retrieved_chunks"] > 0, body
    [citation] = body["citations"]
    assert citation["document_id"] == document_id and citation["locator"]["kind"] == "time"

    if os.environ.get("C06_LOCAL_WHISPER_EVIDENCE"):
        evidence = {
            "test": "current-source-local-whisper-upload-worker-citation",
            "database": "isolated pytest *_test database",
            "document_id": document_id,
            "filename": result["filename"],
            "status": result["status"],
            "chunk_count": result["chunk_count"],
            "transcript_contains_ticket": True,
            "transcript_digit_marker_transcribed_as": "4-821",
            "ambiguous_ticket_number_refused_without_citation": True,
            "citation_locator": citation["locator"],
            "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "model_path": str(model_path),
        }
        evidence_path = Path(os.environ["C06_LOCAL_WHISPER_EVIDENCE"])
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_silent_video_upload_worker_local_ocr_citation_preview_and_acl(client, fake_provider, monkeypatch, tmp_path):
    """A silent video needs local OCR but no speech model; exercise the real API/worker/search path."""
    import importlib.util
    import io
    from pathlib import Path

    if any(importlib.util.find_spec(module) is None for module in ("av", "rapidocr", "onnxruntime")):
        pytest.skip("local PyAV/RapidOCR video OCR dependencies are unavailable")
    import av
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    import rapidocr

    model_dir = Path(rapidocr.__file__).resolve().parent / "models"
    required_models = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx")
    if any(not (model_dir / name).is_file() for name in required_models):
        pytest.skip("local RapidOCR weights are not staged")

    video_path = tmp_path / "qa-silent-inspection.mp4"
    labels = ("QA PUMP PRESSURE 4821", "QA VALVE POSITION 5317", "QA SEAL STATUS 6402")
    output = av.open(str(video_path), mode="w", format="mp4")
    try:
        stream = output.add_stream("mpeg4", rate=1)
        stream.width, stream.height, stream.pix_fmt = 960, 540, "yuv420p"
        font = ImageFont.load_default(size=76)
        for label in labels:
            image = Image.new("RGB", (960, 540), "white")
            draw = ImageDraw.Draw(image)
            bounds = draw.textbbox((0, 0), label, font=font, stroke_width=2)
            draw.text(
                ((960 - (bounds[2] - bounds[0])) // 2, (540 - (bounds[3] - bounds[1])) // 2),
                label, font=font, fill="black", stroke_width=2, stroke_fill="black",
            )
            frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    finally:
        output.close()

    media = av.open(str(video_path), mode="r")
    try:
        assert media.streams.video and not media.streams.audio
    finally:
        media.close()

    settings = get_settings()
    for name, value in {
        "rag_media_enabled": True,
        "rag_media_model_path": "",  # no local Whisper model is required for this silent video
        "rag_media_max_file_mb": 10,
        "rag_media_max_duration_seconds": 10,
        "rag_media_video_frame_ocr_enabled": True,
        "rag_media_video_frame_interval_seconds": 1,
        "rag_media_video_max_frames": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)

    video_bytes = video_path.read_bytes()
    activate_provider(client)
    uploaded = upload(client, video_path.name, video_bytes, visibility="shared", content_type="video/mp4")
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    work = worker(isolate=False)
    assert client.portal.call(work.process_next) is True
    indexed = document(client, document_id)
    assert indexed["status"] == "ready" and indexed["searchable"] and indexed["chunk_count"] >= 1
    async def load_chunks(connection):
        result = await connection.execute(
            text("SELECT content FROM document_chunks WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        return result.scalars().all()

    chunks = run_db(load_chunks)
    recognized = " ".join(chunks).casefold()
    assert all(number in recognized for number in ("4821", "5317", "6402"))

    login(client, USER_EMAIL)
    fake_provider.rag_reply = lambda _message: "QA PUMP PRESSURE 4821 [S1]."
    answer = ask(client, "What is the QA pump pressure?", [document_id]).json()
    assert answer["retrieved_chunks"] > 0 and answer["insufficient_evidence"] is False
    citation = next(item for item in answer["citations"] if item["document_id"] == document_id)
    assert citation["locator"]["kind"] == "video_frame"
    assert citation["locator"]["timestamp_seconds"] in {0, 1, 2}
    assert citation["location"].startswith("Frame at 00:00:")

    preview = f"/api/rag/documents/{document_id}/media"
    login(client, USER2_EMAIL)
    original = client.get(preview)
    assert original.status_code == 200 and original.content == video_bytes
    ranged = client.get(preview, headers={"Range": "bytes=0-31"})
    assert ranged.status_code == 206 and ranged.content == video_bytes[:32]
    assert ranged.headers["content-range"] == f"bytes 0-31/{len(video_bytes)}"

    login(client, ADMIN_EMAIL)
    changed = client.put(
        f"/api/rag/documents/{document_id}/access",
        json={"visibility": "private", "user_emails": [], "group_ids": []},
    )
    assert changed.status_code == 200, changed.text
    login(client, USER2_EMAIL)
    assert client.get(preview).status_code == 404
    refused = ask(client, "What is the QA pump pressure?", [document_id]).json()
    assert refused["retrieved_chunks"] == 0 and refused["citations"] == []
    assert all(number not in refused["answer"] for number in ("4821", "5317", "6402"))
