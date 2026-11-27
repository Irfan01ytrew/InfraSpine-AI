"""Bounded, offline Docker execution for approved Agent Studio regression runs.

Only a filtered source snapshot is mounted into the runner. The runner has no network and reaches one disposable
PostgreSQL test database over a private Unix socket in a uniquely named temporary volume; it cannot reach the host,
production databases, or public network.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COPY_ROOTS = (
    "app",
    "migrations",
    "tests",
    "scripts",
    "docs",
    "evals",
    "integrations/whatsapp-baileys",
    "frontend/src",
    "frontend/dist",
)
_ROOT_FILES = (
    "alembic.ini",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
)
_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "data", "logs", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist-node", "build", ".next",
}
_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 1_500 * 1024 * 1024
_TARGET_ONE = re.compile(
    r"(?:tests|tests/test_[A-Za-z0-9_]+\.py(?:::(?:Test[A-Za-z0-9_]+::)?test_[A-Za-z0-9_]+)?)\Z"
)
_POSTGRES_IMAGE = "postgres:18.6-alpine@sha256:83148c0bc09eac4d598b70a61e1404d0212b76c95c504bfd6c81a4c59ec91792"


def _copy_workspace(destination: Path) -> int:
    """Copy only source/test trees, excluding credentials, runtime data, Git, and dependency caches."""
    copied = 0
    for relative in _ROOT_FILES:
        source = _PROJECT_ROOT / relative
        if source.is_file() and not source.is_symlink():
            size = source.stat().st_size
            if size <= _MAX_FILE_BYTES:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                copied += size

    for relative in _COPY_ROOTS:
        source_root = _PROJECT_ROOT / relative
        if not source_root.is_dir() or source_root.is_symlink():
            continue
        for current, directories, files in os.walk(source_root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if name.casefold() not in _EXCLUDED_DIRS
                and not name.casefold().startswith(".env")
                and not (current_path / name).is_symlink()
            ]
            for filename in files:
                source = current_path / filename
                if source.is_symlink() or filename.casefold().startswith(".env"):
                    continue
                try:
                    size = source.stat().st_size
                    if size > _MAX_FILE_BYTES:
                        continue
                    copied += size
                    if copied > _MAX_WORKSPACE_BYTES:
                        raise ValueError("The allow-listed source snapshot exceeds the 1.5 GB sandbox limit.")
                    relative_file = source.relative_to(_PROJECT_ROOT)
                    target = destination / relative_file
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                except OSError as exc:
                    raise ValueError(f"Could not snapshot the allow-listed source tree ({type(exc).__name__}).") from None
    return copied


def _docker(args: list[str], *, timeout: float, check: bool = False) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("docker")
    if not executable:
        raise ValueError("Docker CLI is not installed or is not available to the Agent Studio service account.")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Docker did not respond within the bounded sandbox timeout.") from None
    except OSError as exc:
        raise ValueError(f"Docker could not be started ({type(exc).__name__}).") from None
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "Docker command failed.").strip()[-1200:]
        raise ValueError(f"Isolated Docker setup failed (exit {result.returncode}): {detail}")
    return result


def _wait_for_postgres(name: str, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _docker(
            ["exec", name, "pg_isready", "-q", "-U", "postgres", "-d", "postgres"],
            timeout=12,
        )
        if result.returncode == 0:
            created = _docker(
                [
                    "exec", name, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                    "-c", "CREATE DATABASE infraspine_ai_test OWNER postgres;",
                ],
                timeout=15,
            )
            if created.returncode:
                raise ValueError("The isolated PostgreSQL test database could not be created.")
            return
        state = _docker(["inspect", "--format", "{{.State.Status}}", name], timeout=12)
        if state.returncode == 0 and state.stdout.strip() != "running":
            logs = _docker(["logs", "--tail", "30", name], timeout=15)
            detail = (logs.stdout + logs.stderr).strip()[-1600:]
            raise ValueError(
                "The disposable PostgreSQL container exited during startup. "
                + (f"Startup log: {detail}" if detail else "No startup log was available.")
            )
        time.sleep(1)
    logs = _docker(["logs", "--tail", "30", name], timeout=15)
    detail = (logs.stdout + logs.stderr).strip()[-1600:]
    suffix = f" Startup log: {detail}" if detail else " No startup log was available."
    raise ValueError(f"The disposable PostgreSQL container did not become ready within {timeout_seconds} seconds.{suffix}")


def _validated_targets(value: Any) -> list[str]:
    if not isinstance(value, str) or len(value) > 800:
        raise ValueError("Provide one or more project-relative pytest targets.")
    targets = value.split(",")
    if not 1 <= len(targets) <= 8 or any(not _TARGET_ONE.fullmatch(item) for item in targets):
        raise ValueError(
            "Choose `tests`, an allow-listed tests/test_*.py file, or up to eight exact ::test_name selectors; "
            "arbitrary shell commands are not accepted."
        )
    if "tests" in targets and len(targets) != 1:
        raise ValueError("The full test suite cannot be combined with additional targets.")
    return targets


def run_pytest(*, target: Any, image: str, timeout_seconds: int = 600) -> dict[str, Any]:
    """Run a fixed pytest command in a writable, disposable source snapshot with no public network."""
    targets = _validated_targets(target)
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}", image):
        raise ValueError("The configured Agent sandbox image name is invalid.")
    if not _PROJECT_ROOT.is_dir():
        raise ValueError("The project source root is unavailable to the Agent Studio service.")

    workspace = Path(tempfile.mkdtemp(prefix="infraspine-agent-docker-")).resolve()
    suffix = uuid.uuid4().hex[:12]
    socket_volume = f"irfanix-test-socket-{suffix}"
    pg_name = f"irfanix-test-db-{suffix}"
    runner_name = f"irfanix-test-runner-{suffix}"
    password = secrets.token_urlsafe(24)
    source_bytes = 0
    try:
        source_bytes = _copy_workspace(workspace)
        for target_item in targets:
            test_path = target_item.split("::", 1)[0]
            selected = (workspace / test_path).resolve()
            if workspace not in selected.parents or (test_path != "tests" and not selected.is_file()):
                raise ValueError("The selected test target does not exist in the isolated source snapshot.")

        _docker(["volume", "create", "--name", socket_volume], timeout=30, check=True)
        _docker(
            [
                "run", "-d", "--name", pg_name, "--network", "none", "--cpus", "1", "--memory", "1g",
                "--pids-limit", "96", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
                "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
                "--tmpfs", "/var/lib/postgresql:rw,nosuid,nodev,size=768m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,uid=70,gid=70,mode=0700",
                "--mount", f"type=volume,source={socket_volume},target=/run/postgresql",
                "--env", "PGDATA=/var/lib/postgresql/data",
                "--env", "POSTGRES_USER=postgres",
                "--env", f"POSTGRES_PASSWORD={password}",
                "--env", "POSTGRES_DB=postgres",
                _POSTGRES_IMAGE,
                "postgres", "-c", "unix_socket_directories=/run/postgresql", "-c", "unix_socket_permissions=0777",
            ],
            timeout=30,
            check=True,
        )
        _wait_for_postgres(pg_name)

        test_database_url = "postgresql+asyncpg://postgres@/infraspine_ai_test?host=/run/postgresql"
        app_database_url = "postgresql+asyncpg://postgres@/postgres?host=/run/postgresql"
        mount = f"type=bind,source={workspace},target=/workspace"
        command = [
            "run", "--rm", "--name", runner_name, "--network", "none",
            "--cpus", "4", "--memory", "3g", "--pids-limit", "256", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "--mount", mount,
            "--mount", f"type=volume,source={socket_volume},target=/run/postgresql,readonly",
            "--workdir", "/workspace",
            "--env", f"DATABASE_URL={app_database_url}",
            "--env", f"TEST_DATABASE_URL={test_database_url}",
            "--env", "MASTER_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "--env", "APP_ENV=testing", "--env", "HF_HUB_OFFLINE=1", "--env", "TRANSFORMERS_OFFLINE=1",
            "--env", "PYTHONDONTWRITEBYTECODE=1", image,
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets,
        ]
        started = time.monotonic()
        try:
            result = _docker(command, timeout=timeout_seconds, check=False)
            output = (result.stdout + result.stderr).strip()
            return {
                "runner": "docker",
                "target": target,
                "passed": result.returncode == 0,
                "exit_code": result.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "workspace_source_bytes": source_bytes,
                "network": "none; temporary PostgreSQL Unix socket only",
                "output": output[-12000:],
            }
        except ValueError as exc:
            if "timeout" in str(exc).casefold():
                _docker(["rm", "-f", runner_name], timeout=10)
                return {
                    "runner": "docker",
                    "target": target,
                    "passed": False,
                    "exit_code": None,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "workspace_source_bytes": source_bytes,
                    "network": "none; temporary PostgreSQL Unix socket only",
                    "output": str(exc),
                }
            raise
    finally:
        with suppress(Exception):
            _docker(["rm", "-f", runner_name], timeout=15)
        with suppress(Exception):
            _docker(["rm", "-fv", pg_name], timeout=15)
        with suppress(Exception):
            _docker(["volume", "rm", socket_volume], timeout=15)
        shutil.rmtree(workspace, ignore_errors=True)
