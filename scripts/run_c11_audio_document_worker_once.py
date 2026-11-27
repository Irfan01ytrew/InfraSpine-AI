"""Process exactly one C06 synthetic audio document on the guarded C11 local review database."""

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
API_PORT = 63011
VITE_PORT = 5182
EMBEDDING_MODEL = (
    ROOT
    / "data/models/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/"
    / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
)
WHISPER_MODEL_CACHE = ROOT / "data/models/faster-whisper-tiny/models--Systran--faster-whisper-tiny/snapshots"
CURRENT_STAGE = "startup"


def expected_whisper_model() -> Path:
    candidates = [
        path.parent
        for path in WHISPER_MODEL_CACHE.glob("*/model.bin")
        if (path.parent / "config.json").is_file()
        and (path.parent / "tokenizer.json").is_file()
        and (path.parent / "vocabulary.txt").is_file()
    ]
    if len(candidates) != 1:
        raise RuntimeError("The project cache must contain exactly one complete local Whisper model snapshot.")
    model = candidates[0].resolve()
    if not model.is_relative_to((ROOT / "data/models").resolve()):
        raise RuntimeError("The local Whisper model escaped the project model cache.")
    return model


def port_owner(port: int) -> int | None:
    for item in psutil.net_connections(kind="inet"):
        if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid:
            return item.pid
    return None


def settings_for_review():
    if not STATE_PATH.is_file():
        raise RuntimeError("The protected C11 review manifest is unavailable.")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    if (
        state.get("review_database") != DATABASE
        or state.get("api_port") != API_PORT
        or state.get("frontend_port") != VITE_PORT
        or state.get("worker_enabled") is not False
        or state.get("llm_mock_mode") is not True
        or state.get("media_enabled") is not True
    ):
        raise RuntimeError("The manifest does not describe the isolated C11 audio review stack.")

    api_pid = int(state["api_pid"])
    process = psutil.Process(api_pid)
    process_env = process.environ()
    database_url = process_env.get("DATABASE_URL", "")
    url = make_url(database_url)
    model_path = expected_whisper_model()
    if (
        "uvicorn app.main:app" not in " ".join(process.cmdline())
        or "127.0.0.1" not in " ".join(process.cmdline())
        or f"--port {API_PORT}" not in " ".join(process.cmdline())
        or Path(process.cwd()).resolve() != ROOT
        or url.database != DATABASE
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not url.port
        or process_env.get("APP_ENV") != "testing"
        or process_env.get("RAG_WORKER_ENABLED", "").casefold() != "false"
        or process_env.get("LLM_MOCK_MODE", "").casefold() != "true"
        or process_env.get("RAG_MEDIA_ENABLED", "").casefold() != "true"
        or process_env.get("RAG_INDEX_SYNC_SECONDS") != "2"
        or Path(process_env.get("RAG_MEDIA_MODEL_PATH", "")).resolve() != model_path
        or not process_env.get("MASTER_ENCRYPTION_KEY")
        or not (process_env.get("RAG_DATA_DIR", "").casefold() == str(REVIEW_ROOT / "data/rag").casefold())
    ):
        raise RuntimeError("The recorded API is not the expected synthetic C11 local-media runtime.")
    listener_pid = port_owner(API_PORT)
    if listener_pid != api_pid:
        listener = psutil.Process(listener_pid) if listener_pid else None
        if listener is None or listener.ppid() != api_pid:
            raise RuntimeError("The API port is not owned by the recorded C11 process or its child.")
    if not psutil.pid_exists(int(state["vite_pid"])) or port_owner(VITE_PORT) != int(state["vite_pid"]):
        raise RuntimeError("The existing C11 Vite listener does not match its protected manifest.")
    if not (EMBEDDING_MODEL / "modules.json").is_file():
        raise RuntimeError("The pinned local embedding model is unavailable.")
    if not (REVIEW_ROOT / ".env").exists():
        # Parser subprocesses use get_settings(); run them from the isolated folder so they cannot load the repo .env.
        pass
    else:
        raise RuntimeError("An unexpected environment file is present in the C11 review root.")

    from app.core.config import Settings

    settings = Settings(
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
        rag_embedding_model=str(EMBEDDING_MODEL),
        rag_models_dir=str(ROOT / "data/models"),
        rag_embedding_allow_download=False,
        rag_parse_isolation=True,
        rag_vector_store="faiss",
        rag_media_enabled=True,
        rag_media_model_path=str(model_path),
        rag_media_device="cpu",
        rag_media_cpu_threads=1,
        rag_media_max_file_mb=20,
        rag_media_max_duration_seconds=120,
        rag_media_segment_seconds=30,
    )
    return settings, process_env


def set_offline_parser_environment(process_env: dict[str, str], settings) -> None:
    # Spawned parser settings resolve only from the explicit synthetic runtime environment. CWD is REVIEW_ROOT,
    # which has no .env, so parser children cannot read the user's repository .env.
    values = {
        "DATABASE_URL": process_env["DATABASE_URL"],
        "MASTER_ENCRYPTION_KEY": process_env["MASTER_ENCRYPTION_KEY"],
        "MASTER_ENCRYPTION_KEY_ID": process_env.get("MASTER_ENCRYPTION_KEY_ID", "v1"),
        "APP_ENV": "testing",
        "RAG_DATA_DIR": str(settings.rag_data_dir),
        "RAG_MEDIA_ENABLED": "true",
        "RAG_MEDIA_MODEL_PATH": settings.rag_media_model_path,
        "RAG_MEDIA_DEVICE": "cpu",
        "RAG_MEDIA_CPU_THREADS": "1",
        "RAG_MEDIA_MAX_FILE_MB": "20",
        "RAG_MEDIA_MAX_DURATION_SECONDS": "120",
        "RAG_MEDIA_SEGMENT_SECONDS": "30",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    os.environ.update(values)


async def run_one(document_id: uuid.UUID) -> dict[str, object]:
    global CURRENT_STAGE
    from app.db.models import DOC_PENDING, DOC_PROCESSING, Document, DocumentChunk
    from app.db.session import create_engine, create_sessionmaker
    from app.rag.components import build_worker
    from app.rag.storage import DocumentStorage

    CURRENT_STAGE = "review runtime validation"
    settings, process_env = settings_for_review()
    set_offline_parser_environment(process_env, settings)
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    worker = build_worker(settings, sessionmaker)
    try:
        async with sessionmaker() as db:
            queued = int(
                await db.scalar(
                    select(func.count(Document.id)).where(Document.status.in_((DOC_PENDING, DOC_PROCESSING)))
                )
                or 0
            )
            document = await db.get(Document, document_id)
            if (
                document is None
                or document.status not in {DOC_PENDING, "ready"}
                or document.file_format != "audio"
                or document.filename != "c06-synthetic-maintenance.wav"
                or (document.status == DOC_PENDING and queued != 1)
                or (document.status == "ready" and queued != 0)
            ):
                raise RuntimeError("The target must be the sole pending C06 audio job or an already-ready document with no queued jobs.")
            should_process = document.status == DOC_PENDING
        processed = False
        if should_process:
            CURRENT_STAGE = "worker startup"
            await worker.startup()
            CURRENT_STAGE = "one-document worker processing"
            prior_cwd = Path.cwd()
            os.chdir(REVIEW_ROOT)
            try:
                processed = await worker.process_next()
            finally:
                os.chdir(prior_cwd)
        CURRENT_STAGE = "audio transcription and source integrity readback"
        async with sessionmaker() as db:
            document = await db.get(Document, document_id)
            rows = (
                await db.execute(
                    select(DocumentChunk.content, DocumentChunk.locator)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).all()
            if document is None:
                raise RuntimeError("The C06 synthetic document disappeared.")
            source_path = DocumentStorage(settings.rag_data_dir / "uploads").path_for(document.storage_key)
            actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            transcript = " ".join(content for content, _locator in rows).casefold()
            transcript_digits = "".join(character for character in transcript if character.isdecimal())
            time_locators = [locator for _content, locator in rows if isinstance(locator, dict) and locator.get("kind") == "time"]
            state = {
                "processed": processed,
                "status": document.status,
                "attempts": document.attempts,
                "error_code": document.error_code,
                "file_format": document.file_format,
                "page_count": document.page_count,
                "chunk_count": len(rows),
                "transcript_has_ticket_label": "ticket number" in transcript,
                "transcript_mentions_ticket_4821": "4821" in transcript_digits,
                "transcript_ticket_text": next(
                    (part.strip() for part in transcript.split(".") if "ticket number" in part), None
                ),
                "time_locator_count": len(time_locators),
                "time_location": time_locators[0].get("label") if time_locators else None,
                "source_sha256_matches_database": actual_sha256 == document.sha256,
                "database_sha256": document.sha256,
            }
        if (
            state["status"] != "ready"
            or state["attempts"] != 1
            or state["file_format"] != "audio"
            or state["chunk_count"] < 1
            or not state["transcript_has_ticket_label"]
            or not state["transcript_mentions_ticket_4821"]
            or state["time_locator_count"] < 1
            or not state["source_sha256_matches_database"]
            or state["error_code"] is not None
        ):
            raise RuntimeError("The local ASR worker result failed a transcript, timestamp, status, or SHA check.")
        return state
    finally:
        worker.release_active()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document_id", type=uuid.UUID)
    args = parser.parse_args()
    if not REVIEW_ROOT.is_dir():
        raise RuntimeError("The C11 review root is unavailable.")
    logging.disable(logging.CRITICAL)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    prior_cwd = Path.cwd()
    try:
        result = asyncio.run(run_one(args.document_id))
        print(json.dumps(result, sort_keys=True))
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
    finally:
        os.chdir(prior_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
