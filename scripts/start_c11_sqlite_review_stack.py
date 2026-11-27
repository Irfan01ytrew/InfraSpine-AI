"""Create a protected synthetic C11 review copy and start a native current-source stack.

The database clone is copied only from the explicitly named, synthetic C14 test database
whose API process is already running. No application .env database target is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
from sqlalchemy.engine import make_url


REPO = Path(__file__).resolve().parents[1]
STACK_STATE = REPO / ".verification-local-review-20261028-c14-agent-runs" / "stack-state.json"
FIXTURE = REPO / ".verification-local-review-20261028-c14-agent-runs" / "credentials.json"
ROOT = REPO / ".verification-local-review-20261028-c11-sqlite-ui"
SOURCE_DATABASE = "infraspine_c14runs_20261028_test"
TARGET_DATABASE = "infraspine_c11sqliteui_20261028_test"
API_PORT = 63011
VITE_PORT = 5182
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_ready(url: str, seconds: int = 60) -> None:
    import urllib.request

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("A newly started local C11 service did not become healthy; inspect its protected local log.")


def run_tool(args: list[str], env: dict[str, str], log_path: Path) -> None:
    with log_path.open("ab") as log:
        completed = subprocess.run(args, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    if completed.returncode:
        raise RuntimeError(f"A PostgreSQL copy utility exited with code {completed.returncode}; its log is local to the protected review folder.")


async def create_target_database(source_url, target: str) -> bool:
    import asyncpg

    connection_args = {
        "host": source_url.host,
        "port": source_url.port or 5432,
        "user": source_url.username,
        "password": source_url.password,
        "timeout": 10,
    }
    admin = await asyncpg.connect(database="postgres", **connection_args)
    try:
        exists = await admin.fetchval("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", target)
        if exists:
            return False
        await admin.execute(f'CREATE DATABASE "{target}" TEMPLATE template0 ENCODING \'UTF8\'')
        return True
    finally:
        await admin.close()


def seed_business_copy(sqlite_file: Path) -> None:
    sqlite_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_file) as connection:
        connection.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, region TEXT NOT NULL, department TEXT NOT NULL, amount NUMERIC NOT NULL, customer TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO orders(region, department, amount, customer) VALUES (?, ?, ?, ?)",
            [
                ("East", "Synthetic QA", 1200.50, "=SUM(1,1)"),
                ("East", "Synthetic QA", 400.00, "Ordinary sample"),
                ("West", "Synthetic QA", 300.00, "Another sample"),
            ],
        )


def main() -> int:
    resuming_own_snapshot = (
        ROOT.exists()
        and (ROOT / "c14-synthetic-clone.dump").is_file()
        and (ROOT / "data" / "business" / "synthetic-reporting.sqlite3").is_file()
        and not (ROOT / "stack-state.json").exists()
    )
    if ROOT.exists() and not resuming_own_snapshot:
        raise RuntimeError("The C11 review folder exists without the exact incomplete snapshot marker; refusing to reuse it.")
    if not STACK_STATE.is_file() or not FIXTURE.is_file():
        raise RuntimeError("The protected C14 synthetic source state or fixture is unavailable.")
    if not port_is_free(API_PORT) or not port_is_free(VITE_PORT):
        raise RuntimeError("The reserved C11 review ports are already in use; no processes were changed.")
    if not (PG_BIN / "pg_dump.exe").is_file() or not (PG_BIN / "pg_restore.exe").is_file():
        raise RuntimeError("The local PostgreSQL copy utilities are unavailable.")

    state = json.loads(STACK_STATE.read_text(encoding="utf-8-sig"))
    api_pid = int(state["apiPid"])
    if state.get("database") != SOURCE_DATABASE or state.get("apiPort") != 63010 or state.get("frontendPort") != 5181:
        raise RuntimeError("The running C14 source identity differs from the documented synthetic stack.")
    inherited = psutil.Process(api_pid).environ()
    if inherited.get("APP_ENV") != "testing" or inherited.get("RAG_WORKER_ENABLED", "").casefold() != "false":
        raise RuntimeError("The source process is not a worker-disabled synthetic QA stack.")
    if not inherited.get("DATABASE_URL") or not inherited.get("MASTER_ENCRYPTION_KEY"):
        raise RuntimeError("The synthetic source process is missing required local runtime settings.")
    source_url = make_url(inherited["DATABASE_URL"])
    if (
        source_url.database != SOURCE_DATABASE
        or source_url.host not in {"localhost", "127.0.0.1", "::1"}
        or not source_url.port
    ):
        raise RuntimeError("The source DB URL did not match the named loopback synthetic test database.")

    if not ROOT.exists():
        ROOT.mkdir(parents=True)
        identity = subprocess.run(["whoami.exe"], check=True, capture_output=True, text=True).stdout.strip()
        acl = subprocess.run(
            ["icacls.exe", str(ROOT), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if acl.returncode:
            raise RuntimeError("Could not apply the current-user-only ACL to the local review folder.")
    for name in ("data", "data/business", "data/rag", "logs", "evidence", "tmp", "downloads"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    sqlite_file = ROOT / "data" / "business" / "synthetic-reporting.sqlite3"
    if not sqlite_file.exists():
        seed_business_copy(sqlite_file)
    else:
        with sqlite3.connect(sqlite_file) as connection:
            sample_count = connection.execute("SELECT count(*) FROM orders").fetchone()[0]
            if sample_count != 3:
                raise RuntimeError("The existing SQLite fixture did not match this C11 synthetic QA copy.")

    dump_file = ROOT / "c14-synthetic-clone.dump"
    copy_env = os.environ.copy()
    copy_env["PGPASSWORD"] = source_url.password or ""
    copy_env["PGAPPNAME"] = "infraspine-c11-synthetic-review-copy"
    dump_log = ROOT / "logs" / "database-clone.log"
    if not dump_file.exists():
        run_tool(
            [
                str(PG_BIN / "pg_dump.exe"), "--format=custom", "--no-owner", "--no-privileges",
                "--host", source_url.host, "--port", str(source_url.port), "--username", source_url.username or "",
                "--dbname", SOURCE_DATABASE, "--file", str(dump_file),
            ],
            copy_env,
            dump_log,
        )
    if not dump_file.is_file() or dump_file.stat().st_size < 100_000:
        raise RuntimeError("The C14 synthetic snapshot is unexpectedly small; it was not restored.")

    target_created = asyncio.run(create_target_database(source_url, TARGET_DATABASE))
    if target_created:
        run_tool(
            [
                str(PG_BIN / "pg_restore.exe"), "--no-owner", "--no-privileges", "--exit-on-error",
                "--host", source_url.host, "--port", str(source_url.port), "--username", source_url.username or "",
                "--dbname", TARGET_DATABASE, str(dump_file),
            ],
            copy_env,
            dump_log,
        )
    # Verify the restore after all objects are loaded. Only schema revision and synthetic row count are emitted.
    import asyncpg

    async def verify_restored():
        conn = await asyncpg.connect(
            host=source_url.host,
            port=source_url.port,
            user=source_url.username,
            password=source_url.password,
            database=TARGET_DATABASE,
            timeout=10,
        )
        try:
            revision = await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
            users = await conn.fetchval("SELECT count(*) FROM users")
            return revision, int(users)
        finally:
            await conn.close()

    revision, user_count = asyncio.run(verify_restored())
    if revision != "0060_agent_profile_run_history" or user_count < 1:
        raise RuntimeError("The restored synthetic source did not pass schema/fixture verification.")

    reporting_catalog = json.dumps(
        {"main.orders": {"order_id": "integer", "region": "text", "department": "text", "amount": "numeric", "customer": "text"}},
        separators=(",", ":"),
    )
    runtime = dict(inherited)
    runtime.update(
        {
            "DATABASE_URL": source_url.set(database=TARGET_DATABASE).render_as_string(hide_password=False),
            "APP_ENV": "testing",
            "LOG_DIR": str(ROOT / "logs" / "api"),
            "RAG_DATA_DIR": str(ROOT / "data" / "rag"),
            "RAG_IMPORT_ROOT": str(ROOT / "data" / "import"),
            "RAG_WORKER_ENABLED": "false",
            "RAG_INGESTION_MODE": "worker",
            # Review workers are invoked one document at a time; the API must still apply/rebuild their
            # PostgreSQL-committed vectors into its FAISS index after each run.
            "RAG_INDEX_SYNC_SECONDS": "2",
            "LLM_MOCK_MODE": "true",
            "SESSION_COOKIE_SECURE": "false",
            "RAG_DATABASE_ANALYSIS_URL": "",
            "RAG_DATABASE_ANALYSIS_SQLITE_PATH": str(sqlite_file),
            "RAG_DATABASE_ANALYSIS_SQLITE_ROOT": str(sqlite_file.parent),
            "RAG_DATABASE_ANALYSIS_CATALOG": reporting_catalog,
            "RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS": "",
            "RAG_DATABASE_ANALYSIS_SOURCE_LABEL": "Synthetic QA reporting copy",
            "RAG_DATABASE_ANALYSIS_STATEMENT_TIMEOUT_MS": "5000",
            "RAG_DATABASE_ANALYSIS_MAX_ROWS": "100",
        }
    )
    api_log = (ROOT / "logs" / "api.log").open("ab")
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=REPO,
        env=runtime,
        stdin=subprocess.DEVNULL,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    api_log.close()
    wait_ready(f"http://127.0.0.1:{API_PORT}/health/ready")

    vite_log = (ROOT / "logs" / "vite.log").open("ab")
    vite_env = dict(runtime)
    vite_env["INFRASPINE_DEV_API_PROXY_TARGET"] = f"http://127.0.0.1:{API_PORT}"
    vite_cli = REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    vite = subprocess.Popen(
        [shutil.which("node") or "node", str(vite_cli), "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
        cwd=REPO / "frontend",
        env=vite_env,
        stdin=subprocess.DEVNULL,
        stdout=vite_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    vite_log.close()
    wait_ready(f"http://127.0.0.1:{VITE_PORT}/admin/")

    digest = hashlib.sha256(dump_file.read_bytes()).hexdigest()
    manifest = {
        "source_database": SOURCE_DATABASE,
        "review_database": TARGET_DATABASE,
        "migration": revision,
        "synthetic_users": user_count,
        "sqlite_source": sqlite_file.name,
        "sqlite_rows": 3,
        "dump_bytes": dump_file.stat().st_size,
        "dump_sha256": digest,
        "api_pid": api.pid,
        "api_port": API_PORT,
        "vite_pid": vite.pid,
        "frontend_port": VITE_PORT,
        "review_url": f"http://127.0.0.1:{VITE_PORT}/admin/",
        "api_health_url": f"http://127.0.0.1:{API_PORT}/health/ready",
        "worker_enabled": False,
        "llm_mock_mode": True,
    }
    (ROOT / "stack-state.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Keep DB URLs, credentials and driver error bodies out of terminal output.
        print(f"C11 review setup failed safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
