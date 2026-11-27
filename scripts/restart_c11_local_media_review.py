"""Restart only the guarded C11 synthetic API with its staged offline Whisper model enabled."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
REVIEW_ROOT = ROOT / ".verification-local-review-20261028-c11-sqlite-ui"
STATE_PATH = REVIEW_ROOT / "stack-state.json"
DATABASE = "infraspine_c11sqliteui_20261028_test"
SOURCE_DATABASE = "infraspine_c14runs_20261028_test"
API_PORT = 63011
VITE_PORT = 5182
MODEL_CACHE = ROOT / "data" / "models" / "faster-whisper-tiny" / "models--Systran--faster-whisper-tiny" / "snapshots"


def local_model_path() -> Path:
    candidates = [
        path.parent
        for path in MODEL_CACHE.glob("*/model.bin")
        if (path.parent / "config.json").is_file()
        and (path.parent / "tokenizer.json").is_file()
        and (path.parent / "vocabulary.txt").is_file()
    ]
    if len(candidates) != 1:
        raise RuntimeError("Expected one fully staged local Faster-Whisper snapshot in the project model cache.")
    model = candidates[0].resolve()
    if not model.is_relative_to((ROOT / "data" / "models").resolve()):
        raise RuntimeError("The local Whisper model escaped the project model cache.")
    return model


def port_owner(port: int) -> int | None:
    for item in psutil.net_connections(kind="inet"):
        if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid:
            return item.pid
    return None


def review_process(process: psutil.Process, expected_pid: int) -> bool:
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


def start_api(environment: dict[str, str]) -> subprocess.Popen:
    log = (REVIEW_ROOT / "logs" / "api.log").open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    finally:
        log.close()


def wait_ready(api: subprocess.Popen, timeout: float = 45) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api.poll() is not None:
            raise RuntimeError("The guarded synthetic API exited during restart.")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/health/ready", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("The guarded synthetic API did not become ready within the bounded startup wait.")


def stop_verified_api(pid: int) -> None:
    parent = psutil.Process(pid)
    parent.terminate()
    parent.wait(timeout=10)
    deadline = time.monotonic() + 10
    while port_owner(API_PORT) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    listener_pid = port_owner(API_PORT)
    if listener_pid is None:
        return
    listener = psutil.Process(listener_pid)
    if listener.ppid() != pid or not review_process(listener, listener.pid):
        raise RuntimeError("The former API listener cannot be positively identified as its owned server child.")
    listener.terminate()
    listener.wait(timeout=10)
    if port_owner(API_PORT) is not None:
        raise RuntimeError("The guarded API port remained occupied after stopping its verified process.")


def main() -> int:
    if not STATE_PATH.is_file():
        raise RuntimeError("The protected C11 review manifest is unavailable.")
    model_path = local_model_path()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    if (
        state.get("review_database") != DATABASE
        or state.get("source_database") != SOURCE_DATABASE
        or state.get("api_port") != API_PORT
        or state.get("frontend_port") != VITE_PORT
        or state.get("worker_enabled") is not False
        or state.get("llm_mock_mode") is not True
    ):
        raise RuntimeError("The C11 manifest does not match the isolated synthetic review stack.")

    pid = int(state["api_pid"])
    process = psutil.Process(pid)
    inherited = process.environ()
    raw_database_url = inherited.get("DATABASE_URL", "")
    database_url = make_url(raw_database_url)
    if (
        inherited.get("APP_ENV") != "testing"
        or inherited.get("RAG_WORKER_ENABLED", "").casefold() != "false"
        or inherited.get("LLM_MOCK_MODE", "").casefold() != "true"
        or not inherited.get("MASTER_ENCRYPTION_KEY")
        or database_url.database != DATABASE
        or database_url.host not in {"localhost", "127.0.0.1", "::1"}
        or not database_url.port
        or not review_process(process, pid)
    ):
        raise RuntimeError("The recorded API is not the exact worker-disabled, mock, loopback C11 process.")

    listener_pid = port_owner(API_PORT)
    if listener_pid != pid:
        listener = psutil.Process(listener_pid) if listener_pid else None
        if listener is None or listener.ppid() != pid or not review_process(listener, listener.pid):
            raise RuntimeError("The API listener is not owned by the recorded C11 review API process.")
    if (
        not psutil.pid_exists(int(state["vite_pid"]))
        or port_owner(VITE_PORT) != int(state["vite_pid"])
    ):
        raise RuntimeError("The existing C11 Vite listener does not match its protected manifest.")

    local_model = str(model_path)
    if (
        inherited.get("RAG_MEDIA_ENABLED", "").casefold() == "true"
        and Path(inherited.get("RAG_MEDIA_MODEL_PATH", "")).resolve() == model_path
        and inherited.get("RAG_INDEX_SYNC_SECONDS") == "2"
    ):
        print(json.dumps({"status": "already_enabled", "database": DATABASE, "api_port": API_PORT, "frontend_port": VITE_PORT}))
        return 0

    runtime = dict(inherited)
    runtime.update(
        {
            "DATABASE_URL": raw_database_url,
            "APP_ENV": "testing",
            "RAG_WORKER_ENABLED": "false",
            "LLM_MOCK_MODE": "true",
            "RAG_MEDIA_ENABLED": "true",
            "RAG_MEDIA_MODEL_PATH": local_model,
            "RAG_MEDIA_DEVICE": "cpu",
            "RAG_MEDIA_CPU_THREADS": "1",
            "RAG_MEDIA_MAX_FILE_MB": "20",
            "RAG_MEDIA_MAX_DURATION_SECONDS": "120",
            "RAG_MEDIA_SEGMENT_SECONDS": "30",
            "RAG_INDEX_SYNC_SECONDS": "2",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
    )

    stop_verified_api(pid)
    api = start_api(runtime)
    try:
        wait_ready(api)
    except Exception:
        if api.poll() is None:
            api.terminate()
            try:
                api.wait(timeout=8)
            except subprocess.TimeoutExpired:
                api.kill()
        fallback = start_api(inherited)
        wait_ready(fallback)
        state["api_pid"] = fallback.pid
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError("Local media API startup failed; the original C11 synthetic API was restored and is healthy.")

    state.update(
        {
            "api_pid": api.pid,
            "media_enabled": True,
            "media_model": str(model_path.relative_to(ROOT)),
            "media_device": "cpu",
            "index_sync_seconds": 2,
        }
    )
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ready", "database": DATABASE, "api_port": API_PORT, "frontend_port": VITE_PORT, "media_model": "local faster-whisper-tiny"}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"C11 local media review stopped safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
