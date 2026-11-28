"""Step G slice 3 — restore verification (restore drill).

A backup is only a backup if it restores. This module restores a
``pg_dump`` backup taken by ``app/continuity/backup.py`` into a throwaway
``*_test`` database and verifies it:

1. The manifest is verified against the files on disk first — a failed
   manifest blocks the restore entirely (plan §6).
2. The target database name must end in ``_test`` and must differ from
   the source database. A production database can never be a restore
   target here.
3. The dump is restored with ``psql`` (``ON_ERROR_STOP=1``) and then
   read back: the ``alembic_version`` head must match the manifest, at
   least one table must exist, and every expected table (when given)
   must be present and readable.
4. The throwaway database is dropped afterwards, best effort.

The module shells out to ``createdb`` / ``psql`` / ``dropdb`` only; the
``runner`` argument exists so tests can substitute fakes. The database
URL is never logged and never appears in an exception message.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from app.continuity.backup import (
    DUMP_FILENAME,
    BackupError,
    Completed,
    Runner,
    find_pg_dump,
    hash_file,
    libpq_url,
    load_manifest,
)

log = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _default_runner(argv: list[str], *, env: dict[str, str] | None = None) -> Completed:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(argv, capture_output=True, text=True, env=merged, timeout=3600)
    return Completed(proc.returncode, proc.stdout, proc.stderr)


def _find_pg_tool(name: str, pg_bin: str | Path | None) -> Path:
    candidates: list[Path] = []
    if pg_bin:
        candidates.append(Path(pg_bin) / name)
    env_bin = os.environ.get("PG_BIN")
    if env_bin:
        candidates.append(Path(env_bin) / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    raise BackupError(f"{name} not found: set PG_BIN or put {name} on PATH")


def _connection_parts(database_url: str) -> dict[str, str]:
    """Host/port/user/password for the ``createdb``/``dropdb`` CLIs."""
    url = make_url(database_url)
    parts: dict[str, str] = {}
    if url.host:
        parts["host"] = url.host
    if url.port:
        parts["port"] = str(url.port)
    if url.username:
        parts["username"] = url.username
    if url.password:
        parts["password"] = url.password
    return parts


def _target_url(database_url: str, database: str) -> str:
    return libpq_url(str(make_url(database_url).set(database=database)))


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def verify_restore(
    *,
    backup_dir: str | Path,
    backup_id: str,
    source_database_url: str,
    target_database: str,
    pg_bin: str | Path | None = None,
    runner: Runner | None = None,
    expected_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Restore ``backup_id`` into throwaway ``target_database`` and verify it.

    Returns ``{"restored", "target_database", "checks", "problems"}``.
    ``restored`` is True only when every check passed. The target
    database is dropped at the end (best effort) so a drill never leaves
    a database behind.
    """
    run = runner or _default_runner
    problems: list[str] = []
    checks: list[dict[str, Any]] = []

    manifest = load_manifest(Path(backup_dir) / backup_id)
    if manifest is None:
        return {"restored": False, "target_database": target_database, "checks": [],
                "problems": [f"Backup {backup_id}: manifest not found"]}
    # Verify THIS manifest against its files on disk: a bad manifest blocks the restore.
    backup_path = Path(backup_dir) / backup_id
    available: dict[str, tuple[int, str]] = {}
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        file_path = backup_path / str(entry.get("path", ""))
        if file_path.is_file():
            available[str(entry["path"])] = hash_file(file_path)
    from app.continuity.health import verify_backup_manifest

    verification = verify_backup_manifest(
        manifest,
        available_files=available,
        now=_manifest_time(manifest),
        expected_alembic_head=str(manifest.get("alembic_head") or ""),
    )
    checks.append(_check("manifest", verification["valid"],
                         "; ".join(verification["problems"]) or "manifest matches files on disk"))
    if not verification["valid"]:
        problems.extend(verification["problems"])
        return {"restored": False, "target_database": target_database, "checks": checks,
                "problems": problems}

    if not target_database.endswith("_test"):
        problems.append(f"Restore target {target_database!r} must end in _test")
        return {"restored": False, "target_database": target_database, "checks": checks,
                "problems": problems}
    try:
        source_name = make_url(source_database_url).database or ""
    except Exception:
        source_name = ""
    if source_name and target_database == source_name:
        problems.append("Restore target must differ from the source database")
        return {"restored": False, "target_database": target_database, "checks": checks,
                "problems": problems}
    for table in expected_tables or []:
        if not _IDENTIFIER_RE.match(table):
            problems.append(f"Invalid expected table name {table!r}")
            return {"restored": False, "target_database": target_database, "checks": checks,
                    "problems": problems}

    dump_path = Path(backup_dir) / backup_id / DUMP_FILENAME
    if not dump_path.is_file():
        problems.append(f"Backup {backup_id}: dump file {DUMP_FILENAME!r} is missing")
        return {"restored": False, "target_database": target_database, "checks": checks,
                "problems": problems}

    find_pg_dump(pg_bin)  # pg_dump is part of the toolchain; fail early if absent
    createdb = _find_pg_tool("createdb", pg_bin)
    psql = _find_pg_tool("psql", pg_bin)
    dropdb = _find_pg_tool("dropdb", pg_bin)
    parts = _connection_parts(source_database_url)
    env = {"PGPASSWORD": parts["password"]} if parts.get("password") else None
    base_args = [f"--host={parts['host']}", f"--port={parts['port']}"] if parts.get("host") else []
    if parts.get("username"):
        base_args.append(f"--username={parts['username']}")

    created = False
    try:
        result = run([str(createdb), *base_args, target_database], env=env)
        if result.returncode != 0:
            problems.append(f"createdb failed for {target_database}: "
                            + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
            return {"restored": False, "target_database": target_database, "checks": checks,
                    "problems": problems}
        created = True
        target_url = _target_url(source_database_url, target_database)
        result = run([str(psql), target_url, "-v", "ON_ERROR_STOP=1", "-q",
                      "-f", str(dump_path)], env=env)
        if result.returncode != 0:
            problems.append("psql restore failed: "
                            + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
            checks.append(_check("restore", False, "psql exited non-zero"))
            return {"restored": False, "target_database": target_database, "checks": checks,
                    "problems": problems}
        checks.append(_check("restore", True, "dump restored without error"))

        def query(sql: str) -> Completed:
            return run([str(psql), target_url, "-t", "-A", "-c", sql], env=env)

        versions = query("SELECT version_num FROM alembic_version ORDER BY version_num")
        if versions.returncode != 0:
            checks.append(_check("alembic_head", False, "alembic_version unreadable"))
            problems.append("Restored database has no readable alembic_version table")
        else:
            found_heads = {line.strip() for line in versions.stdout.splitlines() if line.strip()}
            expected_head = str(manifest.get("alembic_head", ""))
            ok = expected_head in found_heads
            checks.append(_check("alembic_head", ok,
                                 f"restored heads {sorted(found_heads)}, manifest {expected_head!r}"))
            if not ok:
                problems.append(f"Alembic head mismatch after restore "
                                f"(manifest {expected_head!r}, restored {sorted(found_heads)})")

        tables = query("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        count = tables.stdout.strip() if tables.returncode == 0 else ""
        ok = tables.returncode == 0 and count.isdigit() and int(count) > 0
        checks.append(_check("tables_present", ok, f"{count or '?'} tables in public schema"))
        if not ok:
            problems.append("Restored database has no tables in the public schema")

        for table in expected_tables or []:
            rows = query(f"SELECT count(*) FROM {table}")
            ok = rows.returncode == 0 and rows.stdout.strip().isdigit()
            checks.append(_check(f"table:{table}", ok,
                                 f"{rows.stdout.strip() or '?'} rows" if ok else "unreadable"))
            if not ok:
                problems.append(f"Expected table {table!r} is missing or unreadable after restore")
    finally:
        if created:
            dropped = run([str(dropdb), *base_args, target_database], env=env)
            if dropped.returncode != 0:
                log.warning("Restore drill left throwaway database %s behind", target_database)
                problems.append(f"Throwaway database {target_database} was not dropped; remove it manually")

    restored = not problems and all(check["ok"] for check in checks)
    return {"restored": restored, "target_database": target_database,
            "checks": checks, "problems": problems}


def _manifest_time(manifest: dict[str, Any]):
    """Parse a manifest's ``created_at`` for the freshness check (lenient)."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(manifest.get("created_at")))
    except (TypeError, ValueError):
        return datetime.now()
