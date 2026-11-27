"""Custom-format PostgreSQL backups and restore into an empty database only."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url


def _pg_environment(database_url: str) -> tuple[dict[str, str], str]:
    url = make_url(database_url)
    if url.drivername.split("+")[0] != "postgresql" or not url.database:
        raise ValueError("A PostgreSQL DATABASE_URL is required.")
    env = os.environ.copy()
    for key, value in {
        "PGHOST": url.host,
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }.items():
        if value is not None:
            env[key] = value
    query = dict(url.query)
    if query.get("sslmode"):
        env["PGSSLMODE"] = str(query["sslmode"])
    # Never pass the URL or password on pg_dump/pg_restore's command line.
    return env, url.database


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None and sys.platform == "win32":
        install_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        executable = f"{name}.exe"
        candidates = sorted(install_root.glob(f"*/bin/{executable}"), reverse=True)
        path = str(candidates[0]) if candidates else None
    if path is None:
        raise RuntimeError(f"{name} is not installed or is not available on PATH.")
    return path


def verify_backup(path: Path) -> int:
    path = path.resolve(strict=True)
    result = subprocess.run(
        [_tool("pg_restore"), "--list", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore could not verify the backup (exit code {result.returncode}).")
    return sum(1 for line in result.stdout.splitlines() if line.strip() and not line.lstrip().startswith(";"))


def create_backup(database_url: str, destination: Path) -> dict[str, str | int | bool]:
    """Write and verify a non-overwriting custom-format backup; stdout never includes connection details."""
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("The backup destination already exists; choose a new name.")
    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists():
        raise FileExistsError("A partial backup with this name already exists; preserve it and choose a new name.")
    env, database = _pg_environment(database_url)
    try:
        with temporary.open("xb") as output:
            result = subprocess.run(
                [_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges"],
                env=env,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
            output.flush()
            os.fsync(output.fileno())
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed (exit code {result.returncode}).")
        entries = verify_backup(temporary)
        if entries < 1:
            raise RuntimeError("The backup has no catalog entries.")
        if destination.exists():
            raise FileExistsError("The backup destination appeared while this backup was running.")
        os.rename(temporary, destination)  # same-directory rename; on Windows refuses an existing destination
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            while block := source.read(8 * 1024 * 1024):
                digest.update(block)
        return {
            "database": database,
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": digest.hexdigest(),
            "catalog_entries": entries,
            "verified": True,
        }
    except BaseException:
        # The only removed path is this invocation's newly-created incomplete .part file.
        temporary.unlink(missing_ok=True)
        raise


async def restore_into_empty_database(database_url: str, backup: Path) -> dict[str, str | int | bool]:
    """Restore only when the configured target has no user relations; never drops or cleans existing data."""
    from sqlalchemy import text

    from app.db.session import create_engine

    backup = backup.expanduser().resolve(strict=True)
    entries = verify_backup(backup)
    env, database = _pg_environment(database_url)
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            user_relations = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')"
                )
            )
        if user_relations:
            raise RuntimeError(
                "Restore refused: the configured PostgreSQL database contains user tables or sequences. "
                "Create a new empty database and point DATABASE_URL to it first."
            )
        with backup.open("rb") as source:
            result = subprocess.run(
                [
                    _tool("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
                    "--dbname", database,
                ],
                env=env,
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(f"pg_restore failed (exit code {result.returncode}); no cleanup or drop was attempted.")
        return {"database": database, "path": str(backup), "catalog_entries": entries, "restored": True}
    finally:
        await engine.dispose()
