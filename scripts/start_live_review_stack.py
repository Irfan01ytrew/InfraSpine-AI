"""Start the synthetic C11 review stack against a live AI provider (no mock mode).

Uses the existing isolated synthetic database on the local PostgreSQL server named in `.env`,
migrates it to head, then starts a worker-disabled API and the current-source Vite UI. The AI
provider and key are configured by the owner in the app's AI Providers page; nothing here reads
or prints a provider credential.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".verification-live-review-20261029"
DATABASE = "infraspine_livereview_20261029_test"
BUSINESS_SEED = REPO / ".verification-local-review-20261028-c11-sqlite-ui/data/business/synthetic-reporting.sqlite3"
CLUSTER_URL = "postgresql+asyncpg://postgres@127.0.0.1:55929/postgres"
API_PORT = 63011
# The review UI listens on the local network so colleagues can open it; the API itself stays on loopback
# and is reached through the UI server's /api proxy. Set INFRASPINE_REVIEW_UI_HOST=127.0.0.1 to keep it local.
UI_HOST = os.environ.get("INFRASPINE_REVIEW_UI_HOST", "0.0.0.0")
VITE_PORT = 5182
EMBEDDING_MODEL = REPO / "data/models/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MEDIA_MODEL = REPO / "data/models/faster-whisper-tiny/models--Systran--faster-whisper-tiny/snapshots/d90ca5fe260221311c53c58e660288d3deb8d356"


def wait_ready(url: str, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{url} did not become ready")


def spawn(args: list[str], cwd: Path, env: dict[str, str], log_name: str) -> subprocess.Popen:
    log = (ROOT / "logs" / log_name).open("ab")
    try:
        return subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, close_fds=True,
        )
    finally:
        log.close()


def review_runtime(database_url: str) -> dict[str, str]:
    """Process environment shared by the review API and its one-shot document workers."""
    runtime = {k: v for k, v in os.environ.items() if k != "LLM_MOCK_MODE"}
    runtime.update({
        "DATABASE_URL": database_url,
        "APP_ENV": "testing",
        "LOG_DIR": str(ROOT / "logs" / "api"),
        "RAG_DATA_DIR": str(ROOT / "data" / "rag"),
        "RAG_IMPORT_ROOT": str(ROOT / "data" / "import"),
        "RAG_WORKER_ENABLED": "false",
        "RAG_INGESTION_MODE": "worker",
        "RAG_INDEX_SYNC_SECONDS": "2",
        "SESSION_COOKIE_SECURE": "false",
        "RAG_MEDIA_ENABLED": "true",
        # Public market research (Gemini + Google Search grounding) is opt-in per installation.
        "RAG_EXTERNAL_RESEARCH_ENABLED": "true",
        # A local Ollama model answers when the active cloud provider fails (balance, rate limit, outage).
        # The admin adds it in AI providers as "Local Ollama" (model qwen2.5:7b; the 3B model often omits citations)
        # with base URL http://127.0.0.1:11434/v1.
        "PROVIDER_TRUSTED_PRIVATE_ORIGINS": "http://127.0.0.1:11434",
        # A local test receiver for webhooks (scripts/webhook_receiver.py); real receivers must be public HTTPS.
        "WEBHOOK_TRUSTED_PRIVATE_ORIGINS": "http://127.0.0.1:63099",
        "LLM_FALLBACK_PROVIDER": "custom-local-ollama-qwen2-5-7b",
        # Read Urdu scans too (RapidOCR Arabic-script recognizer; model verified by checksum in RapidOCR's folder).
        "RAG_OCR_URDU": "true",
        "RAG_MEDIA_MODEL_PATH": str(MEDIA_MODEL),
        "RAG_MEDIA_DEVICE": "cpu",
        "RAG_MEDIA_CPU_THREADS": "2",
        "RAG_MEDIA_MAX_FILE_MB": "20",
        "RAG_MEDIA_MAX_DURATION_SECONDS": "120",
        "RAG_MEDIA_SEGMENT_SECONDS": "30",
        "RAG_MODELS_DIR": str(REPO / "data" / "models"),
        "RAG_EMBEDDING_MODEL": str(EMBEDDING_MODEL),
        "RAG_EMBEDDING_ALLOW_DOWNLOAD": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "RAG_DATABASE_ANALYSIS_SQLITE_PATH": str(ROOT / "data" / "business" / "synthetic-reporting.sqlite3"),
        "RAG_DATABASE_ANALYSIS_SQLITE_ROOT": str(ROOT / "data" / "business"),
        "RAG_DATABASE_ANALYSIS_CATALOG": json.dumps({"main.orders": {"order_id": "integer", "region": "text", "department": "text", "amount": "numeric", "customer": "text"}}),
        "RAG_DATABASE_ANALYSIS_SOURCE_LABEL": "Synthetic QA reporting copy",
    })
    return runtime


async def ensure_database(server) -> None:
    """Create the dedicated *_test database on first run; never touches any other database."""
    import asyncpg

    admin = await asyncpg.connect(host=server.host, port=server.port or 5432, user=server.username, password=server.password, database="postgres", timeout=10)
    try:
        if not await admin.fetchval("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", DATABASE):
            await admin.execute(f'CREATE DATABASE "{DATABASE}" TEMPLATE template0 ENCODING \'UTF8\'')
    finally:
        await admin.close()


def database_url() -> str:
    return make_url(CLUSTER_URL).set(database=DATABASE).render_as_string(hide_password=False)


def main() -> int:
    # The disposable loopback-only PostgreSQL 18 review cluster; the live application database is never used.
    server = make_url(CLUSTER_URL)
    if server.host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("The review stack only runs against a loopback PostgreSQL server.")
    if not DATABASE.endswith("_test"):
        raise RuntimeError("The review database must be a dedicated *_test database.")
    database_url = server.set(database=DATABASE).render_as_string(hide_password=False)
    asyncio.run(ensure_database(server))
    for sub in ("logs/api", "data/rag", "data/import", "data/business"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    business = ROOT / "data" / "business" / "synthetic-reporting.sqlite3"
    if not business.exists():
        shutil.copy2(BUSINESS_SEED, business)

    runtime = review_runtime(database_url)
    (ROOT / "logs" / "api").mkdir(parents=True, exist_ok=True)

    migrate = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO, env=runtime, capture_output=True, text=True)
    if migrate.returncode:
        sys.stderr.write(migrate.stderr[-4000:].replace(server.password or "<no-password>", "***"))
        raise RuntimeError("Migrating the review database failed.")

    api = spawn([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)], REPO, runtime, "api.log")
    wait_ready(f"http://127.0.0.1:{API_PORT}/health/ready")
    # A continuous document worker, so uploads made in the review UI are processed without manual steps.
    worker = spawn([sys.executable, "-m", "app.worker"], REPO, runtime, "worker.log")
    vite_env = dict(runtime, INFRASPINE_DEV_API_PROXY_TARGET=f"http://127.0.0.1:{API_PORT}")
    vite = spawn([shutil.which("node") or "node", str(REPO / "frontend/node_modules/vite/bin/vite.js"), "--host", UI_HOST, "--port", str(VITE_PORT), "--strictPort"], REPO / "frontend", vite_env, "vite.log")
    wait_ready(f"http://127.0.0.1:{VITE_PORT}/admin/")

    state = {"review_database": DATABASE, "api_pid": api.pid, "worker_pid": worker.pid, "api_port": API_PORT, "vite_pid": vite.pid, "frontend_port": VITE_PORT,
             "review_url": f"http://127.0.0.1:{VITE_PORT}/admin/", "worker_enabled": False, "llm_mode": "live provider configured in-app",
             "media_enabled": True, "media_model": str(MEDIA_MODEL.relative_to(REPO))}
    (ROOT / "stack-state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"review_url": f"http://127.0.0.1:{VITE_PORT}/admin/", "api": f"http://127.0.0.1:{API_PORT}/health/ready", "api_pid": api.pid, "vite_pid": vite.pid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
