"""Run one isolated C11 synthetic document job without polling unrelated worker queues."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import uuid

import psutil
from sqlalchemy import func, select
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REVIEW_ROOT = ROOT / ".verification-local-review-20261028-c11-sqlite-ui"
STATE_PATH = REVIEW_ROOT / "stack-state.json"
DATABASE = "infraspine_c11sqliteui_20261028_test"
CURRENT_STAGE = "startup"
PINNED_MODEL = (
    ROOT
    / "data/models/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/"
    / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
)


def _settings(model_unavailable: bool):
    if not STATE_PATH.is_file():
        raise RuntimeError("review manifest missing")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    if (
        state.get("review_database") != DATABASE
        or state.get("api_port") != 63011
        or state.get("frontend_port") != 5182
        or state.get("worker_enabled") is not False
        or state.get("llm_mock_mode") is not True
    ):
        raise RuntimeError("review manifest mismatch")
    process_env = psutil.Process(int(state["api_pid"])).environ()
    database_url = process_env.get("DATABASE_URL", "")
    url = make_url(database_url)
    if (
        url.database != DATABASE
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not url.port
        or process_env.get("APP_ENV") != "testing"
        or process_env.get("RAG_WORKER_ENABLED", "").casefold() != "false"
        or process_env.get("LLM_MOCK_MODE", "").casefold() != "true"
        or not process_env.get("MASTER_ENCRYPTION_KEY")
    ):
        raise RuntimeError("API process is not the expected synthetic C11 runtime")

    from app.core.config import Settings

    model_name = str(PINNED_MODEL) if not model_unavailable else str(REVIEW_ROOT / "absent-model-for-c05-test")
    if model_unavailable and Path(model_name).exists():
        raise RuntimeError("synthetic absent-model path unexpectedly exists")
    if not model_unavailable and not (PINNED_MODEL / "modules.json").is_file():
        raise RuntimeError("pinned local model is unavailable")

    return Settings(
        _env_file=None,
        database_url=database_url,
        master_encryption_key=process_env["MASTER_ENCRYPTION_KEY"],
        master_encryption_key_id=process_env.get("MASTER_ENCRYPTION_KEY_ID", "v1"),
        app_env="testing",
        log_level="ERROR",
        llm_mock_mode=True,
        database_pool_size=1,
        database_pool_max_overflow=0,
        rag_worker_enabled=False,
        rag_ingestion_mode="worker",
        rag_data_dir=Path(process_env["RAG_DATA_DIR"]),
        rag_import_root=Path(process_env.get("RAG_IMPORT_ROOT", str(REVIEW_ROOT / "data/import"))),
        rag_embedding_model=model_name,
        rag_models_dir=process_env["RAG_MODELS_DIR"],
        rag_embedding_allow_download=False,
        rag_parse_isolation=process_env.get("RAG_PARSE_ISOLATION", "false").casefold() == "true",
        rag_vector_store="faiss",
    )


async def _run(document_id: uuid.UUID, model_unavailable: bool) -> dict[str, object]:
    global CURRENT_STAGE
    CURRENT_STAGE = "worker module imports"
    from app.db.models import DOC_PENDING, DOC_PROCESSING, Document, DocumentChunk
    from app.db.session import create_engine, create_sessionmaker
    from app.rag.components import build_worker
    from app.rag.storage import DocumentStorage

    CURRENT_STAGE = "review runtime validation"
    settings = _settings(model_unavailable)
    engine = create_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0
    )
    sessionmaker = create_sessionmaker(engine)
    worker = build_worker(settings, sessionmaker)
    try:
        async with sessionmaker() as db:
            queued = int(
                await db.scalar(
                    select(func.count(Document.id)).where(
                        Document.status.in_((DOC_PENDING, DOC_PROCESSING))
                    )
                )
                or 0
            )
            document = await db.get(Document, document_id)
            if document is None or document.status != DOC_PENDING or queued != 1:
                raise RuntimeError("target is not the sole pending synthetic document")
        CURRENT_STAGE = "worker startup"
        await worker.startup()
        CURRENT_STAGE = "document queue processing"
        processed = await worker.process_next()
        CURRENT_STAGE = "document result readback"
        async with sessionmaker() as db:
            document = await db.get(Document, document_id)
            chunks = list(
                (
                    await db.scalars(
                        select(DocumentChunk.content).where(DocumentChunk.document_id == document_id)
                    )
                ).all()
            )
            if document is None:
                raise RuntimeError("target document disappeared")
            source_path = DocumentStorage(settings.rag_data_dir / "uploads").path_for(document.storage_key)
            actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            state = {
                "processed": processed,
                "status": document.status,
                "attempts": document.attempts,
                "error_code": document.error_code,
                "version": document.version,
                "page_count": document.page_count,
                "chunk_count": len(chunks),
                "extracted_synthetic_fact": any("30 calendar days after delivery" in text for text in chunks),
                "source_sha256_matches_database": actual_sha256 == document.sha256,
                "database_sha256": document.sha256,
            }
        expected = "failed" if model_unavailable else "ready"
        if state["status"] != expected or not state["source_sha256_matches_database"]:
            raise RuntimeError("document worker result did not match expected state")
        if model_unavailable and state["error_code"] != "embedding_model_unavailable":
            raise RuntimeError("missing model did not produce the expected safe failure")
        if not model_unavailable and (
            state["chunk_count"] < 1 or not state["extracted_synthetic_fact"] or state["error_code"] is not None
        ):
            raise RuntimeError("local model did not produce a searchable synthetic passage")
        return state
    finally:
        worker.release_active()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document_id", type=uuid.UUID)
    parser.add_argument("--model-unavailable", action="store_true")
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    logging.disable(logging.CRITICAL)
    try:
        state = asyncio.run(_run(args.document_id, args.model_unavailable))
        print(json.dumps(state, sort_keys=True))
        return 0
    except Exception as exc:
        payload = {"error_type": type(exc).__name__, "stage": CURRENT_STAGE}
        if isinstance(exc, ModuleNotFoundError):
            payload["missing_module"] = exc.name
        if hasattr(exc, "errors") and callable(exc.errors):
            payload["validation_fields"] = [
                {"loc": error.get("loc", ()), "type": error.get("type", "unknown")}
                for error in exc.errors()
            ]
        print(json.dumps(payload), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
