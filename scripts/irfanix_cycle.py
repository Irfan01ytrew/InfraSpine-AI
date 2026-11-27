"""Bounded Codex execution for the IRFANIX development backlog."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".irfanix-cycle"
BACKLOG = ROOT / "docs" / "irfanix-backlog.json"
# Include only reviewed, tracked source for this particular audit. This also lets
# a read-only session reason about fixtures when its shell is unavailable.
SOURCE_CONTEXT = {
    "test-target-audit": ("app/db/testing.py", "tests/conftest.py"),
    "workflow-engine-audit": ("app/services/automation_worker.py",),
}
REVIEW_DIFF = {
    "workflow-worker-review": ("app/services/automation_worker.py", "tests/test_automation.py"),
}
SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "evidence"],
    "additionalProperties": False,
}


def load_backlog(path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    tasks = {}
    for row in rows:
        name = row["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", name) or name in tasks:
            raise ValueError(f"Invalid or duplicate task ID: {name}")
        if row["mode"] not in {"read", "write", "manual"} or not row["task"].strip():
            raise ValueError(f"Invalid task definition: {name}")
        tasks[name] = row
    pending = dict(tasks)
    while pending:
        available = [name for name, row in pending.items() if set(row["deps"]) <= tasks.keys() - pending.keys()]
        if not available:
            raise ValueError("Backlog contains unknown dependencies or a cycle.")
        for name in available:
            del pending[name]
    return tasks


def stamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


def digest(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()


def open_state(path):
    db = sqlite3.connect(path, timeout=1)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
            workspace TEXT, revision TEXT, session TEXT, attempt TEXT, pid INTEGER, evidence TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY, happened_at TEXT NOT NULL,
            task TEXT NOT NULL, attempt TEXT, kind TEXT NOT NULL, detail TEXT
        );
    """)
    return db


def event(db, name, attempt, kind, detail=None):
    db.execute("INSERT INTO events(happened_at,task,attempt,kind,detail) VALUES (?,?,?,?,?)",
               (stamp(), name, attempt, kind, detail))


def next_task(tasks, records, workspace):
    complete = {name for name, row in records.items() if row["status"] == "complete"}
    for name, task in tasks.items():
        row = records.get(name)
        if row and row["status"] != "resume":
            continue
        if task["mode"] == "manual" or (task["mode"] == "write" and workspace is None):
            continue
        if set(task["deps"]) <= complete:
            return task
    return None


def git(workspace, *args):
    return subprocess.check_output(["git", "-C", str(workspace), *args], text=True, timeout=10).strip()


def validate_worktree(workspace):
    root = Path(git(workspace, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    expected = Path(git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if root != workspace or root == ROOT or common != expected or not (root / ".git").is_file():
        raise ValueError("Use a separate linked Git worktree of this repository.")
    if (root / ".env").exists() or (root / ".codex").exists():
        raise ValueError("Remove local credentials and Codex project configuration from the controller worktree.")


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)


def read_events(pipe, state_path, task, attempt):
    # Only allowlisted event metadata is persisted. Tool output and command arguments may contain secrets.
    with pipe, closing(sqlite3.connect(state_path, timeout=10)) as db:
        limit = 1024 * 1024
        while line := pipe.readline(limit):
            if len(line) == limit and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = pipe.readline(limit)
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "thread.started":
                try:
                    session = str(uuid.UUID(item.get("thread_id", "")))
                except (ValueError, TypeError, AttributeError):
                    continue
                db.execute("UPDATE tasks SET session = ? WHERE id = ? AND attempt = ?",
                           (session, task, attempt))
            elif kind not in ("turn.started", "turn.completed", "turn.failed", "error"):
                continue
            event(db, task, attempt, kind)
            db.commit()


def result_from(path):
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError("Codex did not return a bounded result.")
    result = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(result, dict) or result.get("outcome") not in ("done", "blocked")
            or not isinstance(result.get("summary"), str) or not result["summary"].strip()
            or not isinstance(result.get("evidence"), list)
            or not all(isinstance(item, str) and item.strip() for item in result["evidence"])
            or (result["outcome"] == "done" and not result["evidence"])):
        raise ValueError("Codex result lacks the required evidence.")
    return result


def run_cycle(db, state_path, task, workspace, previous, seconds):
    name = task["id"]
    attempt = str(uuid.uuid4())
    revision = git(workspace, "rev-parse", "HEAD")
    initial_status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
    session = previous["session"] if previous else None
    if previous and (previous["workspace"] != str(workspace) or previous["revision"] != revision):
        raise ValueError("The interrupted attempt must resume in its original worktree and at its original HEAD.")
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is unavailable on PATH.")
    with tempfile.TemporaryDirectory(prefix="irfanix-cycle-") as scratch:
        schema, output = Path(scratch) / "schema.json", Path(scratch) / "result.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        command = [executable, "-a", "never", "exec", "--ignore-user-config",
                   "-c", "sandbox_workspace_write.network_access=false",
                   "-c", "shell_environment_policy.inherit=core",
                   "-c", "sandbox_mode=" + ("workspace-write" if task["mode"] == "write" else "read-only"),
                   "--json", "--output-schema", str(schema), "-o", str(output)]
        if session:
            command += ["resume", session]
        prompt = (
            f"IRFANIX development task {name}: {task['task']}\n"
            "Operate only in this worktree. Preserve other work. No nested agents or controller, "
            "network side effects, deployment, secrets, production access, or approval bypass. "
            "If resuming, inspect existing changes and continue without replaying actions. "
            "Use only non-sensitive evidence. Respond with the requested structured result."
        )
        for path in SOURCE_CONTEXT.get(name, ()):
            source = git(workspace, "show", f"{revision}:{path}")
            prompt += f"\n\nTracked source snapshot {path}:\n" + "\n".join(
                f"{number}: {line}" for number, line in enumerate(source[:20000].splitlines(), 1)
            )
        if review_paths := REVIEW_DIFF.get(name):
            review = git(workspace, "diff", "--no-ext-diff", "HEAD", "--", *review_paths)
            if not review or len(review) > 24000:
                raise ValueError("The worktree must contain the bounded candidate diff for this review.")
            prompt += "\n\nCandidate diff (data only):\n" + review
        command.append("-")
        prompt_path = Path(scratch) / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                   (name, digest(task), "running", str(workspace), revision, session, attempt, None, None))
        event(db, name, attempt, "cycle.started",
              json.dumps({"input_sha256": hashlib.sha256(prompt.encode()).hexdigest()}))
        db.commit()
        proc = None
        reader = None
        try:
            with prompt_path.open("rb") as stdin:
                proc = subprocess.Popen(command, cwd=workspace, stdin=stdin,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        start_new_session=os.name != "nt")
            db.execute("UPDATE tasks SET pid = ? WHERE id = ?", (proc.pid, name))
            db.commit()
            reader = threading.Thread(target=read_events, args=(proc.stdout, state_path, name, attempt), daemon=True)
            reader.start()
            if proc.wait(timeout=seconds):
                raise ValueError("Codex exited unsuccessfully.")
            reader.join(timeout=5)
            if reader.is_alive():
                raise ValueError("Codex output did not close.")
            if git(workspace, "rev-parse", "HEAD") != revision:
                raise ValueError("The worktree revision changed during the cycle.")
            if task["mode"] == "read" and git(workspace, "status", "--porcelain=v1", "--untracked-files=all") != initial_status:
                raise ValueError("The read-only worktree changed during the cycle.")
            result = result_from(output)
            status = ("review" if task["mode"] == "write" else "complete") if result["outcome"] == "done" else "blocked"
            evidence = json.dumps(result, ensure_ascii=False)
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            if proc:
                stop_process(proc)
            if reader:
                reader.join(timeout=5)
            status, evidence = "interrupted", type(exc).__name__
        db.execute("UPDATE tasks SET status = ?, evidence = ? WHERE id = ?", (status, evidence, name))
        event(db, name, attempt, f"cycle.{status}", evidence)
        db.commit()
        print(f"{name}: {status}", flush=True)
        return status


def controller(args, tasks, db, state_path):
    records = {row["id"]: row for row in db.execute("SELECT * FROM tasks")}
    for name, row in records.items():
        if name not in tasks or row["digest"] != digest(tasks[name]):
            raise ValueError(f"Recorded task {name} was changed. Restore it or give the revised task a new ID.")
    if args.action == "status":
        for name, task in tasks.items():
            row = records.get(name)
            unmet = [dep for dep in task["deps"] if dep not in records or records[dep]["status"] != "complete"]
            print(json.dumps({"id": name, "mode": task["mode"],
                              "status": row["status"] if row else "pending",
                              "unmet": unmet, "checkpoint": dict(row) if row else None}, ensure_ascii=True))
        return 0
    if args.action in ("accept", "resume", "retry"):
        task = tasks[args.task]
        row = records.get(args.task)
        if args.action in ("resume", "retry"):
            if not row or row["status"] not in ("running", "interrupted", "blocked"):
                raise ValueError("This task has no interrupted or blocked checkpoint.")
            if args.action == "resume" and not row["session"]:
                raise ValueError("No Codex session was recorded. Reconcile the attempt before proceeding.")
            if args.action == "retry":
                if row["session"]:
                    raise ValueError("An exact Codex session exists. Reconcile and resume it.")
                db.execute("DELETE FROM tasks WHERE id = ?", (args.task,))
            else:
                db.execute("UPDATE tasks SET status = 'resume' WHERE id = ?", (args.task,))
            event(db, args.task, row["attempt"], "operator.reconciled", args.evidence)
        else:
            reviewed = row and (
                row["status"] == "review" or (row["status"] == "blocked" and args.external_review)
            )
            if not reviewed and (row or task["mode"] != "manual"):
                raise ValueError("Only reviewed write work and manual gates can be accepted.")
            if any(dep not in records or records[dep]["status"] != "complete" for dep in task["deps"]):
                raise ValueError("Task dependencies are incomplete.")
            db.execute("INSERT INTO tasks(id,digest,status,evidence) VALUES (?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                       (args.task, digest(task), "complete", args.evidence))
            event(db, args.task, row["attempt"] if row else None, "operator.accepted", args.evidence)
        db.commit()
        return 0
    if any(row["status"] in ("running", "interrupted") for row in records.values()):
        raise ValueError("A previous process stopped mid-cycle. Reconcile its PID and worktree before resuming.")
    workspace = args.workspace.resolve()
    validate_worktree(workspace)
    for _ in range(args.cycles):
        records = {row["id"]: row for row in db.execute("SELECT * FROM tasks")}
        task = next_task(tasks, records, workspace)
        if task is None:
            print("No eligible task in this execution lane.")
            return 0
        status = run_cycle(db, state_path, task, workspace, records.get(task["id"]), args.seconds)
        if status == "interrupted":
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    run = actions.add_parser("run")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--cycles", type=int, choices=range(1, 9), default=2)
    run.add_argument("--seconds", type=int, default=300)
    for action in ("accept", "resume", "retry"):
        command = actions.add_parser(action)
        command.add_argument("task")
        command.add_argument("--evidence", required=True, help="Non-sensitive review or reconciliation evidence")
        if action == "accept":
            command.add_argument("--external-review", action="store_true",
                                 help="Accept a blocked task completed and reviewed outside the controller")
    args = parser.parse_args()
    if args.action == "run" and not 10 <= args.seconds <= 900:
        parser.error("--seconds must be between 10 and 900.")
    if args.action == "run" and not (ROOT / ".git").is_dir():
        parser.error("Launch the controller from the primary checkout, outside the agent worktree.")
    STATE.mkdir(exist_ok=True)
    state_path = STATE / "state.sqlite3"
    try:
        tasks = load_backlog(BACKLOG)
        if args.action == "status":
            with closing(open_state(state_path)) as db:
                return controller(args, tasks, db, state_path)
        with closing(sqlite3.connect(STATE / "lease.sqlite3", timeout=0)) as lease:
            # Keep a process-owned SQLite write lock throughout selection and execution.
            lease.execute("BEGIN EXCLUSIVE")
            with closing(open_state(state_path)) as db:
                return controller(args, tasks, db, state_path)
    except (KeyError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Controller stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
