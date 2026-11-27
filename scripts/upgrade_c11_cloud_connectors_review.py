"""Safely apply current migrations and restart only the named synthetic C11 review API.

The existing PostgreSQL cluster is preserved. This script refuses every database except
the explicitly named local synthetic review clone and refuses an unowned listener.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
REVIEW_ROOT = ROOT / ".verification-local-review-20261028-c11-sqlite-ui"
STATE_PATH = REVIEW_ROOT / "stack-state.json"
DATABASE = "infraspine_c11sqliteui_20261028_test"
SOURCE_DATABASE = "infraspine_c14runs_20261028_test"
API_PORT = 63011
VITE_PORT = 5182
REVISION = "0061_persist_cloud_connectors"


def port_owner(port: int) -> int | None:
    for item in psutil.net_connections(kind="inet"):
        if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid:
            return item.pid
    return None


async def current_revision(database_url: str) -> str:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        host=url.host, port=url.port or 5432, user=url.username, password=url.password,
        database=url.database, timeout=8,
    )
    try:
        return await connection.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
    finally:
        await connection.close()


def is_review_server(process: psutil.Process, expected_pid: int) -> bool:
    try:
        args = " ".join(process.cmdline())
        return (
            process.pid == expected_pid
            and "uvicorn app.main:app" in args
            and "127.0.0.1" in args
            and f"--port {API_PORT}" in args
            and Path(process.cwd()).resolve() == ROOT
        )
    except (psutil.Error, OSError):
        return False


def main() -> int:
    if not STATE_PATH.is_file():
        raise RuntimeError("The protected C11 review stack manifest is unavailable.")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    if (
        state.get("review_database") != DATABASE
        or state.get("source_database") != SOURCE_DATABASE
        or state.get("api_port") != API_PORT
        or state.get("frontend_port") != VITE_PORT
        or state.get("worker_enabled") is not False
    ):
        raise RuntimeError("The C11 stack manifest did not match the isolated synthetic review stack.")
    pid = int(state["api_pid"])
    parent = psutil.Process(pid)
    inherited = parent.environ()
    if inherited.get("APP_ENV") != "testing" or inherited.get("RAG_WORKER_ENABLED", "").casefold() != "false":
        raise RuntimeError("The recorded C11 API process is not a worker-disabled test process.")
    raw_database_url = inherited.get("DATABASE_URL", "")
    url = make_url(raw_database_url)
    if (
        url.database != DATABASE
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not url.port
        or not inherited.get("MASTER_ENCRYPTION_KEY")
    ):
        raise RuntimeError("The recorded process does not target the exact loopback C11 synthetic database.")
    if not is_review_server(parent, pid):
        raise RuntimeError("The recorded C11 API process identity did not match its loopback command and workspace.")
    listener_pid = port_owner(API_PORT)
    if listener_pid != pid:
        listener = psutil.Process(listener_pid) if listener_pid else None
        if listener is None or listener.ppid() != pid or not is_review_server(listener, listener.pid):
            raise RuntimeError("The API listener is not the recorded C11 process or its verified server child.")
    if not psutil.pid_exists(int(state["vite_pid"])) or port_owner(VITE_PORT) != int(state["vite_pid"]):
        raise RuntimeError("The existing C11 frontend listener does not match the stack manifest.")

    before = asyncio.run(current_revision(raw_database_url))
    if before not in {"0060_agent_profile_run_history", REVISION}:
        raise RuntimeError("The C11 synthetic database has an unexpected migration revision.")
    if before != REVISION:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.attributes["database_url"] = raw_database_url
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
    if asyncio.run(current_revision(raw_database_url)) != REVISION:
        raise RuntimeError("The isolated C11 migration did not reach the expected revision.")

    # Stop only the process positively identified as the recorded loopback C11 API.
    parent.terminate()
    parent.wait(timeout=10)
    until = time.monotonic() + 10
    while port_owner(API_PORT) is not None and time.monotonic() < until:
        time.sleep(0.1)
    if port_owner(API_PORT) is not None:
        owner = psutil.Process(port_owner(API_PORT))
        expected_child = owner.ppid() == pid and is_review_server(owner, owner.pid)
        if not expected_child:
            raise RuntimeError("The former C11 API child did not exit and cannot be safely identified for restart.")
        owner.terminate()
        owner.wait(timeout=10)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", API_PORT)) == 0:
            raise RuntimeError("The C11 API port remains occupied after stopping its verified process.")

    runtime = dict(inherited)
    runtime.update({"DATABASE_URL": raw_database_url, "APP_ENV": "testing", "RAG_WORKER_ENABLED": "false"})
    log = (REVIEW_ROOT / "logs" / "api.log").open("ab")
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT,
        env=runtime,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    log.close()
    deadline = time.monotonic() + 45
    import urllib.request

    while time.monotonic() < deadline:
        if api.poll() is not None:
            raise RuntimeError("The newly started C11 API exited; inspect its protected local review log.")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/health/ready", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError("The new C11 API did not become healthy within its bounded startup wait.")

    state.update({"migration": REVISION, "api_pid": api.pid})
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"database": DATABASE, "migration": REVISION, "api_pid": api.pid, "api_port": API_PORT, "frontend_port": VITE_PORT}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"C11 synthetic connector upgrade stopped safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
