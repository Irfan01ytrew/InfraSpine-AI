"""Step G slice 2 — pg_dump backups + backup-freshness check.

Slice 1 (`app/continuity/health.py`) defined the backup manifest as pure
functions. This module does the real work: it runs ``pg_dump`` for a
database, writes the dump plus a SHA-256 manifest, and checks a backup
directory for freshness.

Safety rules (plan §6):

* The database URL is never logged and never appears in an exception
  message — it carries a password.
* A manifest that fails verification blocks any restore (see
  ``app/continuity/restore.py``); this module only reports problems, it
  never deletes or overwrites an existing backup.
* Backup ids are file-safe names only (``bk-YYYYMMDD-HHMMSS``); a
  traversal attempt (``..`` or a separator) is rejected.
* ``pg_dump`` is invoked by path with an argument list — no shell, no
  string interpolation of the URL.

The ``runner`` argument exists so tests can substitute a fake ``pg_dump``
without a database; production callers pass nothing and get
``subprocess.run``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from app.continuity.health import (
    DEFAULT_BACKUP_MAX_AGE,
    backup_freshness,
    build_backup_manifest,
    verify_backup_manifest,
)

log = logging.getLogger(__name__)

DUMP_FILENAME = "db.dump"
MANIFEST_FILENAME = "manifest.json"
_BACKUP_ID_RE = re.compile(r"^bk-[0-9]{8}-[0-9]{6}(-[0-9]+)?$")


class BackupError(RuntimeError):
    """A backup could not be taken, found, or checked. Never carries a URL."""


@dataclass
class Completed:
    """Minimal ``subprocess.CompletedProcess`` shape returned by a runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, argv: list[str], *, env: dict[str, str] | None = None) -> Completed: ...


def _default_runner(argv: list[str], *, env: dict[str, str] | None = None) -> Completed:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(argv, capture_output=True, text=True, env=merged, timeout=3600)
    return Completed(proc.returncode, proc.stdout, proc.stderr)


def _sha256_file(path: Path) -> tuple[int, str]:
    return hash_file(path)


def hash_file(path: Path) -> tuple[int, str]:
    """``(size_bytes, sha256 hex)`` of a file, streamed in 1 MiB chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _backup_id(now: datetime) -> str:
    return "bk-" + now.strftime("%Y%m%d-%H%M%S")


def _validate_backup_id(backup_id: str) -> None:
    if not _BACKUP_ID_RE.match(backup_id):
        raise BackupError(f"Invalid backup id: {backup_id!r}")


def find_pg_dump(pg_bin: str | Path | None = None) -> Path:
    """Locate the ``pg_dump`` binary: explicit dir, ``PG_BIN`` env, then PATH."""
    candidates: list[Path] = []
    if pg_bin:
        candidates.append(Path(pg_bin) / "pg_dump")
    env_bin = os.environ.get("PG_BIN")
    if env_bin:
        candidates.append(Path(env_bin) / "pg_dump")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("pg_dump")
    if found:
        return Path(found)
    raise BackupError("pg_dump not found: set PG_BIN or put pg_dump on PATH")


def current_alembic_head() -> str:
    """The single Alembic head revision of this checkout (for the manifest)."""
    root = Path(__file__).resolve().parents[2]
    config = AlembicConfig(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        raise BackupError(f"Expected exactly one Alembic head, found {len(heads)}")
    return heads[0]


def create_backup(
    *,
    backup_dir: str | Path,
    database_url: str,
    alembic_head: str,
    pg_bin: str | Path | None = None,
    runner: Runner | None = None,
    now: datetime | None = None,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """Take a ``pg_dump`` backup and write its manifest. Returns the manifest.

    Layout: ``<backup_dir>/<backup_id>/db.dump`` + ``manifest.json``.
    Raises :class:`BackupError` (never carrying the URL) when ``pg_dump``
    fails, and leaves a failed backup directory behind for inspection.
    """
    moment = now or datetime.now(timezone.utc)
    backup_id = _backup_id(moment) if backup_id is None else backup_id
    _validate_backup_id(backup_id)
    if not database_url:
        raise BackupError("A database URL is required to take a backup")
    pg_dump = find_pg_dump(pg_bin)
    run = runner or _default_runner
    target = Path(backup_dir) / backup_id
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise BackupError(f"Backup {backup_id} already exists") from None
    dump_path = target / DUMP_FILENAME
    result = run([str(pg_dump), libpq_url(database_url), "--no-owner", "--no-privileges",
                  "-f", str(dump_path)])
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise BackupError(
            f"pg_dump failed for backup {backup_id}: " + (detail[-1] if detail else "unknown error")
        )
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise BackupError(f"pg_dump produced no dump for backup {backup_id}")
    size, digest = _sha256_file(dump_path)
    manifest = build_backup_manifest(
        backup_id=backup_id,
        created_at=moment,
        alembic_head=alembic_head,
        database=_database_name(database_url),
        files=[{"path": DUMP_FILENAME, "size_bytes": size, "sha256": digest}],
    )
    (target / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Backup %s written (%d bytes)", backup_id, size)
    return manifest


def _database_name(database_url: str) -> str:
    """Database name from a URL, without ever exposing credentials."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(database_url).database or ""
    except Exception:
        return ""


def libpq_url(database_url: str) -> str:
    """A ``postgresql://`` URL for libpq tools (pg_dump/psql/createdb).

    SQLAlchemy URLs carry a driver suffix (``postgresql+asyncpg://``)
    that libpq does not understand — passed through verbatim, pg_dump
    treats the whole string as a database name and falls back to the
    default socket and OS user. This strips the driver while keeping
    host, port, user, password and database.
    """
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def load_manifest(backup_path: str | Path) -> dict[str, Any] | None:
    """Read a backup's manifest, or ``None`` when it is missing/unreadable."""
    path = Path(backup_path) / MANIFEST_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def latest_manifest(backup_dir: str | Path) -> tuple[str, dict[str, Any]] | None:
    """The newest valid manifest under ``backup_dir`` (by ``created_at``)."""
    root = Path(backup_dir)
    if not root.is_dir():
        return None
    best: tuple[str, dict[str, Any]] | None = None
    best_created = ""
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = load_manifest(child)
        if manifest is None:
            continue
        created = str(manifest.get("created_at", ""))
        if best is None or created > best_created:
            best, best_created = (child.name, manifest), created
    return best


def check_backup(
    backup_dir: str | Path,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
    expected_alembic_head: str | None = None,
) -> dict[str, Any]:
    """Freshness + verification of the latest backup in ``backup_dir``.

    Returns ``{"freshness", "backup_id", "manifest", "verification"}``.
    ``freshness`` is ``fresh`` / ``stale`` / ``missing`` (see
    ``health.backup_freshness``); ``verification`` is the
    ``health.verify_backup_manifest`` report against the files actually on
    disk. A backup that is missing or fails verification must block a
    restore — callers must check both fields.
    """
    found = latest_manifest(backup_dir)
    if found is None:
        return {
            "freshness": "missing",
            "backup_id": None,
            "manifest": None,
            "verification": {"valid": False, "problems": ["No backup manifest found"], "checked": 0},
        }
    backup_id, manifest = found
    backup_path = Path(backup_dir) / backup_id
    available: dict[str, tuple[int, str]] = {}
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        path = backup_path / str(entry.get("path", ""))
        if path.is_file():
            available[str(entry["path"])] = _sha256_file(path)
    verification = verify_backup_manifest(
        manifest,
        available_files=available,
        now=now,
        max_age=max_age,
        expected_alembic_head=expected_alembic_head,
    )
    return {
        "freshness": backup_freshness(manifest, now=now, max_age=max_age),
        "backup_id": backup_id,
        "manifest": manifest,
        "verification": verification,
    }
