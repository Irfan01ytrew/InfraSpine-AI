"""Process-level recovery acceptance for scheduled folder synchronization."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import Document, FolderSyncNotification, FolderSyncRun, FolderSyncSource, User
from app.main import app
from app.rag.bulk_import import _lock_key
from app.services.audit import AuditContext
from app.services.folder_sync import create_source
from tests.conftest import ADMIN_EMAIL


def _safe_worker_diagnostics(output: str) -> str:
    output = re.sub(r"(?i)postgres(?:ql)?(?:\+\w+)?://[^\s'\"]+", "postgresql://[redacted]", output)
    lines = [
        line.strip()
        for line in output.splitlines()
        if (
            "Traceback" in line
            or "ERROR" in line
            or "WARNING" in line
            or "DBAPIError" in line
            or "asyncpg" in line
            or "ImportError" in line
            or "ModuleNotFound" in line
            or "No module named" in line
            or "faiss.loader" in line
            or "app.worker" in line
            or "Processed " in line
        )
    ]
    return " | ".join(lines[-20:])[:2400]


def test_real_worker_process_restart_recovers_interrupted_folder_sync(client, tmp_path: Path, monkeypatch):
    """Kill the real worker while a second file is blocked; restart and prove resumable completion."""
    root = tmp_path / "import-root"
    source_folder = root / "process-recovery"
    source_folder.mkdir(parents=True)
    (source_folder / "01-first.txt").write_text(
        "Synthetic first source committed before worker termination.", encoding="utf-8"
    )
    (source_folder / "02-second.txt").write_text(
        "Synthetic second source imported after worker restart.", encoding="utf-8"
    )
    monkeypatch.setattr(get_settings(), "rag_import_root", root)

    async def create_source_record():
        async with app.state.sessionmaker() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            assert admin is not None and admin.role == "admin" and admin.is_active
            created = await create_source(
                db,
                admin,
                get_settings(),
                name="Worker process recovery",
                relative_path="process-recovery",
                visibility="shared",
                department_group_id=None,
                interval_minutes=5,
                ctx=AuditContext(admin.id, admin.email, None, None),
            )
            return {"id": str(created.id), "source_id": created.source_id}

    source = client.portal.call(create_source_record)
    source_uri = f"folder-import://{source['source_id']}/01-first.txt"
    model_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "models"
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    )
    assert (model_path / "modules.json").is_file(), "Pinned local embedding model must be staged."

    worker_env = os.environ.copy()
    worker_env.update(
        {
            "APP_ENV": "testing",
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAG_DATA_DIR": str(get_settings().rag_data_dir.resolve()),
            "RAG_IMPORT_ROOT": str(root),
            "RAG_MODELS_DIR": str(model_path.parents[2]),
            "RAG_EMBEDDING_MODEL": str(model_path),
            "RAG_EMBEDDING_ALLOW_DOWNLOAD": "false",
            "RAG_INGESTION_MODE": "worker",
            "RAG_WORKER_ENABLED": "false",
            "RAG_PARSE_ISOLATION": "false",
            "RAG_VECTOR_STORE": "faiss",
            "RAG_INDEX_SYNC_SECONDS": "0",
            "DATABASE_POOL_SIZE": "1",
            "DATABASE_POOL_MAX_OVERFLOW": "0",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LOG_DIR": "",
        }
    )
    preflight = subprocess.run(
        [sys.executable, "-m", "app.worker", "--check"],
        cwd=Path(__file__).resolve().parents[1],
        env=worker_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    preflight_error_types = sorted(
        set(re.findall(r"\b[A-Za-z_][A-Za-z0-9]*(?:Error|Exception)\b", preflight.stdout + preflight.stderr))
    )
    assert preflight.returncode == 0, (
        f"Exact worker subprocess preflight exit={preflight.returncode}; errors={preflight_error_types}; "
        f"diagnostics={_safe_worker_diagnostics(preflight.stdout + preflight.stderr)}"
    )
    lock_key = _lock_key(f"folder-import://{source['source_id']}/02-second.txt")

    async def make_source_due():
        async with app.state.sessionmaker() as db:
            await db.execute(
                update(FolderSyncSource)
                .where(FolderSyncSource.id == source["id"])
                .values(next_sync_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()

    async def acquire_second_file_lock():
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        connection = await engine.connect()
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        await connection.commit()
        return engine, connection

    async def release_second_file_lock(engine, connection):
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
        await connection.commit()
        await connection.close()
        await engine.dispose()

    async def first_file_committed():
        async with app.state.sessionmaker() as db:
            return await db.scalar(select(Document.id).where(Document.source_uri == source_uri)) is not None

    async def worker_waiting_on_advisory_lock():
        async with app.state.sessionmaker() as db:
            return bool(
                await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event = 'advisory' "
                        "AND query ILIKE '%pg_advisory_xact_lock%')"
                    )
                )
            )

    async def expire_source_lease():
        async with app.state.sessionmaker() as db:
            await db.execute(
                update(FolderSyncSource)
                .where(FolderSyncSource.id == source["id"])
                .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()

    async def read_recovery_evidence():
        async with app.state.sessionmaker() as db:
            runs = list(
                (
                    await db.scalars(
                        select(FolderSyncRun)
                        .join(FolderSyncSource, FolderSyncSource.id == FolderSyncRun.source_id)
                        .where(FolderSyncSource.id == source["id"])
                        .order_by(FolderSyncRun.started_at)
                    )
                ).all()
            )
            documents = list(
                (
                    await db.scalars(
                        select(Document).where(Document.source_uri.like(f"folder-import://{source['source_id']}/%"))
                    )
                ).all()
            )
            notifications = list(
                (
                    await db.scalars(
                        select(FolderSyncNotification).where(FolderSyncNotification.run_id.in_([run.id for run in runs]))
                    )
                ).all()
            )
            return runs, documents, notifications

    client.portal.call(make_source_due)
    engine, lock_connection = client.portal.call(acquire_second_file_lock)
    interrupted_worker: subprocess.Popen | None = None
    try:
        interrupted_worker = subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--once"],
            cwd=Path(__file__).resolve().parents[1],
            env=worker_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 45
        observed_blocked_partial_commit = False
        last_worker_state = (False, False)
        worker_error_types: list[str] = []
        worker_diagnostics = ""
        while time.monotonic() < deadline:
            if interrupted_worker.poll() is not None:
                output, _ = interrupted_worker.communicate(timeout=5)
                worker_error_types = sorted(
                    set(re.findall(r"\b[A-Za-z_][A-Za-z0-9]*(?:Error|Exception)\b", output or ""))
                )
                worker_diagnostics = _safe_worker_diagnostics(output or "")
                break
            has_first = client.portal.call(first_file_committed)
            is_waiting = client.portal.call(worker_waiting_on_advisory_lock)
            last_worker_state = (has_first, is_waiting)
            if has_first and is_waiting:
                observed_blocked_partial_commit = True
                break
            time.sleep(0.05)
        if not observed_blocked_partial_commit:
            if interrupted_worker.poll() is None:
                interrupted_worker.kill()
            output, _ = interrupted_worker.communicate(timeout=5)
            worker_error_types = sorted(
                set(re.findall(r"\b[A-Za-z_][A-Za-z0-9]*(?:Error|Exception)\b", output or ""))
            )
            worker_diagnostics = _safe_worker_diagnostics(output or "")
        assert observed_blocked_partial_commit, (
            "Worker did not commit file 1 and block on file 2 in time; "
            f"process_returncode={interrupted_worker.poll()}, error_types={worker_error_types}, "
            f"first_file_committed={last_worker_state[0]}, waiting_on_second_file={last_worker_state[1]}, "
            f"diagnostics={worker_diagnostics}"
        )

        interrupted_worker.kill()
        interrupted_worker.wait(timeout=15)
        assert interrupted_worker.returncode != 0, "The acceptance must terminate the worker OS process."
    finally:
        if interrupted_worker is not None and interrupted_worker.poll() is None:
            interrupted_worker.kill()
            interrupted_worker.wait(timeout=15)
        client.portal.call(release_second_file_lock, engine, lock_connection)

    # A dead process leaves its DB lease running. Expire only this synthetic source's lease
    # so the normal, unchanged worker recovery branch can exercise the retry deterministically.
    client.portal.call(make_source_due)
    client.portal.call(expire_source_lease)
    restarted = subprocess.run(
        [sys.executable, "-m", "app.worker", "--once"],
        cwd=Path(__file__).resolve().parents[1],
        env=worker_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    worker_error_types = sorted(
        set(re.findall(r"\b[A-Za-z_][A-Za-z0-9]*(?:Error|Exception)\b", restarted.stdout + restarted.stderr))
    )
    assert restarted.returncode == 0, f"Restarted worker exit={restarted.returncode}; error_types={worker_error_types}"

    runs, documents, notifications = client.portal.call(read_recovery_evidence)
    assert [(run.status, run.error_code, run.attempt) for run in runs] == [
        ("failed", "lease_expired", 1),
        ("completed", None, 2),
    ]
    assert len(documents) == 2
    assert sum(document.source_uri.endswith("01-first.txt") for document in documents) == 1
    assert sum(document.source_uri.endswith("02-second.txt") for document in documents) == 1
    assert {notice.run_id for notice in notifications} == {runs[0].id, runs[1].id}
