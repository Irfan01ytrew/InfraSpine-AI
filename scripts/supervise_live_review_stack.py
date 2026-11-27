"""Keep the local review stack running: restart the API, document worker or UI when one exits.

The review processes are started detached and are not Windows services, so a crashed or killed
process used to stay down until someone restarted it by hand; the UI then showed 502 errors for
every API call. This watchdog checks each process every few seconds, restarts what is down with the
same command and environment as `start_live_review_stack.py`, and records each restart in
`logs/supervisor.log`. It only touches the processes named in `stack-state.json`.

Run it detached:  python scripts/supervise_live_review_stack.py
Stop it:          taskkill /PID <supervisor_pid from stack-state.json> /F
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from start_live_review_stack import API_PORT, REPO, ROOT, UI_HOST, VITE_PORT, database_url, review_runtime, spawn  # noqa: E402

CHECK_SECONDS = 10
# The supervisor runs windowless (pythonw); without this every console tool it starts opens a CMD window.
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# A process that is alive but not answering for this long is treated as hung and restarted.
UNRESPONSIVE_SECONDS = 60
STATE = ROOT / "stack-state.json"
LOG = ROOT / "logs" / "supervisor.log"
LOCK = ROOT / "logs" / "supervisor.lock"
# The disposable review PostgreSQL cluster; after a reboot it is down until something starts it.
PG_BIN = Path(r"C:/Program Files/PostgreSQL/18/bin")
PG_CLUSTER = REPO / ".verification-local-review-20261029-c16-pg-isolated"
PG_PORT = 55929


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, creationflags=HIDDEN)
    return str(pid) in result.stdout


def stop(pid: int | None) -> None:
    if pid and alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=HIDDEN)


def fresh_runtime(fallback: dict[str, str]) -> dict[str, str]:
    """Re-read the review settings so a restart picks up configuration changes without restarting the supervisor."""
    try:
        import importlib

        import start_live_review_stack as stack

        importlib.reload(stack)
        return stack.review_runtime(stack.database_url())
    except Exception as exc:  # keep running on the settings it started with
        log(f"could not reload review settings ({type(exc).__name__}); using the previous ones")
        return fallback


def start(name: str, runtime: dict[str, str]) -> int:
    if name == "api":
        process = spawn([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)], REPO, runtime, "api.log")
    elif name == "worker":
        process = spawn([sys.executable, "-m", "app.worker"], REPO, runtime, "worker.log")
    else:
        vite_env = dict(runtime, INFRASPINE_DEV_API_PROXY_TARGET=f"http://127.0.0.1:{API_PORT}")
        node = shutil.which("node") or "node"
        process = spawn([node, str(REPO / "frontend/node_modules/vite/bin/vite.js"), "--host", UI_HOST, "--port", str(VITE_PORT), "--strictPort"], REPO / "frontend", vite_env, "vite.log")
    return process.pid


def database_up() -> bool:
    result = subprocess.run([str(PG_BIN / "pg_isready.exe"), "-h", "127.0.0.1", "-p", str(PG_PORT)], capture_output=True, creationflags=HIDDEN)
    return result.returncode == 0


def start_database() -> None:
    """Start the cluster without waiting; the next check confirms it is accepting connections."""
    options = f"-h 127.0.0.1 -p {PG_PORT} -c max_connections=40 -c listen_addresses=127.0.0.1"
    log_file = (PG_CLUSTER / "postgres.log").open("ab")
    try:
        subprocess.Popen(
            [str(PG_BIN / "pg_ctl.exe"), "-D", str(PG_CLUSTER / "cluster"), "-l", str(PG_CLUSTER / "postgres.log"), "-o", options, "start"],
            cwd=PG_CLUSTER, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | HIDDEN, close_fds=True,
        )
    finally:
        log_file.close()


def another_supervisor_running() -> bool:
    """A scheduled task starts this script every few minutes; only the first copy keeps running."""
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid != os.getpid() and alive(pid)


def main() -> int:
    if another_supervisor_running():
        return 0
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    runtime = review_runtime(database_url())
    state = json.loads(STATE.read_text(encoding="utf-8"))
    state["supervisor_pid"] = os.getpid()
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    log(f"supervisor started pid={os.getpid()}")
    checks = {
        "api": lambda: answers(f"http://127.0.0.1:{API_PORT}/health"),
        "vite": lambda: answers(f"http://127.0.0.1:{VITE_PORT}/admin/"),
        "worker": lambda: alive(state.get("worker_pid")),
    }
    down_since: dict[str, float] = {}
    while True:
        try:
            if not database_up():
                log("database was down; starting the review PostgreSQL cluster")
                start_database()
                time.sleep(CHECK_SECONDS)
                continue
            state = check_once(checks, down_since, runtime, state)
        except Exception as exc:  # the watchdog must outlive any single failed check
            log(f"check failed: {type(exc).__name__}: {exc}")
        time.sleep(CHECK_SECONDS)


def check_once(checks, down_since: dict[str, float], runtime: dict[str, str], state: dict) -> dict:
    for name, healthy in checks.items():
        if healthy():
            down_since.pop(name, None)
            continue
        pid = state.get(f"{name}_pid")
        # Give a live process time to finish starting (the API loads models) before calling it hung.
        first = down_since.setdefault(name, time.monotonic())
        if alive(pid) and time.monotonic() - first < UNRESPONSIVE_SECONDS:
            continue
        stop(pid)
        new_pid = start(name, fresh_runtime(runtime))
        state = json.loads(STATE.read_text(encoding="utf-8"))
        state[f"{name}_pid"] = new_pid
        STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        log(f"{name} was down (old pid={pid}); restarted as pid={new_pid}")
        down_since[name] = time.monotonic()
    return state


if __name__ == "__main__":
    raise SystemExit(main())
