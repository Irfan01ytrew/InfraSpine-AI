"""Small, explicitly non-qualifying measurement of the native folder-sync worker path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, FolderSyncRun, FolderSyncSource, User
from app.main import app
from app.services.audit import AuditContext
from app.services.folder_sync import create_source
from tests.conftest import ADMIN_EMAIL
from tests.test_folder_sync_worker_process import _safe_worker_diagnostics


def test_bounded_native_worker_ingests_a_small_synthetic_source(client, tmp_path: Path, monkeypatch):
    root = tmp_path / "import-root"
    source_folder = root / "bounded-worker-throughput"
    source_folder.mkdir(parents=True)
    file_count = 8
    payload = (
        "Synthetic facilities maintenance review. The operator records the inspection date, observed condition, "
        "required follow-up, and the source passage used to support the record. "
    ) * 5
    for index in range(file_count):
        (source_folder / f"sample-{index + 1:02d}.txt").write_text(
            f"Synthetic record {index + 1}. {payload}", encoding="utf-8"
        )
    total_bytes = sum(path.stat().st_size for path in source_folder.iterdir())
    monkeypatch.setattr(get_settings(), "rag_import_root", root)

    async def create_source_record():
        async with app.state.sessionmaker() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            assert admin is not None and admin.role == "admin" and admin.is_active
            source = await create_source(
                db,
                admin,
                get_settings(),
                name="F08 bounded worker throughput",
                relative_path="bounded-worker-throughput",
                visibility="shared",
                department_group_id=None,
                interval_minutes=5,
                ctx=AuditContext(admin.id, admin.email, None, None),
            )
            return {"id": str(source.id), "source_id": source.source_id}

    source = client.portal.call(create_source_record)
    model_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "models"
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    )
    assert (model_path / "modules.json").is_file(), "The pinned local embedding model must already be staged."

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
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LOG_DIR": "",
        }
    )
    started = time.perf_counter()
    invocations = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "app.worker", "--once"],
            cwd=Path(__file__).resolve().parents[1],
            env=worker_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        invocations.append(result.returncode)
        diagnostics = _safe_worker_diagnostics(result.stdout + result.stderr)
        assert result.returncode == 0, f"Native worker exit={result.returncode}; safe diagnostics={diagnostics}"
    elapsed = time.perf_counter() - started

    async def read_measurement():
        async with app.state.sessionmaker() as db:
            source_row = await db.get(FolderSyncSource, source["id"])
            runs = list(
                (
                    await db.scalars(
                        select(FolderSyncRun).where(FolderSyncRun.source_id == source["id"])
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
            return source_row, runs, documents

    source_row, runs, documents = client.portal.call(read_measurement)
    assert source_row is not None and source_row.last_error_code is None
    assert len(runs) == 1 and runs[0].status == "completed"
    assert (runs[0].scanned, runs[0].created, runs[0].failed) == (file_count, file_count, 0)
    assert len(documents) == file_count and all(doc.status == "ready" for doc in documents)

    files_per_second = file_count / max(elapsed, 1e-9)
    evidence = {
        "result": "PASS",
        "scope": "single native Windows --once worker, one fresh synthetic folder-sync source, no customer data",
        "worker_invocations": len(invocations),
        "worker_exit_codes": invocations,
        "files": file_count,
        "bytes": total_bytes,
        "elapsed_seconds": round(elapsed, 3),
        "end_to_end_files_per_second": round(files_per_second, 3),
        "run_status": runs[0].status,
        "scanned": runs[0].scanned,
        "created": runs[0].created,
        "failed": runs[0].failed,
        "ready_documents": len(documents),
        "embedding_backend": "pinned local MiniLM + FAISS; CPU threads bounded to 1",
        "release_qualification": "target-hardware throughput and recovery remain pending",
    }
    evidence_path = os.environ.get("INFRASPINE_F08_BENCH_EVIDENCE")
    if evidence_path:
        destination = Path(evidence_path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(
        "F08_NATIVE_WORKER_BOUNDED_MEASUREMENT="
        f"PASS; files={file_count}; bytes={total_bytes}; elapsed_s={elapsed:.3f}; "
        f"files_per_s={files_per_second:.3f}; created={len(documents)}; failed=0; target_hardware_gate=PENDING"
    )
