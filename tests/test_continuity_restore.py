"""Step G slice 3 — restore verification (restore drill) tests.

Most tests use a fake runner (no database): the manifest gate, the
``*_test`` target guard, and the verification read-back are all
exercised without PostgreSQL. One integration test at the bottom runs
a real ``pg_dump`` + ``psql`` restore against the throwaway test
database and is skipped when the PostgreSQL client tools are absent.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.continuity.backup import Completed, create_backup
from app.continuity.health import sha256_hex
from app.continuity.restore import verify_restore

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)
DUMP = b"PGDUMP-SYNTHETIC-BACKUP-BYTES"
SOURCE_URL = "postgresql://restore_user:secret-pass@127.0.0.1:5432/infraspine_ai_test"
TARGET = "infraspine_stepg_restore_test"


@pytest.fixture
def pg_bin(tmp_path):
    for name in ("pg_dump", "createdb", "psql", "dropdb"):
        (tmp_path / name).write_bytes(b"#!/bin/sh\nexit 0\n")
        (tmp_path / name).chmod(0o755)
    return tmp_path


@pytest.fixture
def backup_dir(tmp_path, pg_bin):
    root = tmp_path / "backups"

    def fake_run(argv, *, env=None):
        out = Path(argv[argv.index("-f") + 1])
        out.write_bytes(DUMP)
        return Completed(0, "", "")

    create_backup(backup_dir=root, database_url=SOURCE_URL, alembic_head="0103",
                  pg_bin=pg_bin, runner=fake_run, now=NOW, backup_id="bk-20261128-010000")
    return root


def _scripted_runner(script):
    """A fake runner answering from ``script``: list of (predicate, Completed)."""
    calls = []

    def run(argv, *, env=None):
        calls.append(argv)
        for predicate, result in script:
            if predicate(argv):
                return result
        return Completed(0, "", "")

    run.calls = calls
    return run


def _is_psql_query(argv, needle):
    return argv and "psql" in argv[0] and "-c" in argv and needle in argv[argv.index("-c") + 1]


def _happy_script():
    return [
        (lambda a: "createdb" in a[0], Completed(0, "", "")),
        (lambda a: "psql" in a[0] and "-f" in a, Completed(0, "", "")),
        (lambda a: _is_psql_query(a, "alembic_version"), Completed(0, "0103\n", "")),
        (lambda a: _is_psql_query(a, "information_schema.tables"), Completed(0, "42\n", "")),
        (lambda a: _is_psql_query(a, "FROM users"), Completed(0, "3\n", "")),
        (lambda a: _is_psql_query(a, "FROM bc_tenants"), Completed(0, "1\n", "")),
        (lambda a: "dropdb" in a[0], Completed(0, "", "")),
    ]


def _verify(backup_dir, pg_bin, runner, **overrides):
    kwargs = {"backup_dir": backup_dir, "backup_id": "bk-20261128-010000",
              "source_database_url": SOURCE_URL, "target_database": TARGET,
              "pg_bin": pg_bin, "runner": runner}
    kwargs.update(overrides)
    return verify_restore(**kwargs)


def test_restore_happy_path_with_fake_tools(backup_dir, pg_bin):
    runner = _scripted_runner(_happy_script())
    result = _verify(backup_dir, pg_bin, runner, expected_tables=["users", "bc_tenants"])
    assert result["restored"] is True, result
    assert result["problems"] == []
    assert {c["name"] for c in result["checks"]} == {
        "manifest", "restore", "alembic_head", "tables_present", "table:users", "table:bc_tenants"}
    assert all(c["ok"] for c in result["checks"])
    assert any("dropdb" in a[0] for a in runner.calls), "throwaway DB must be dropped"


def test_restore_blocked_when_manifest_tampered(backup_dir, pg_bin):
    (backup_dir / "bk-20261128-010000" / "db.dump").write_bytes(b"tampered")
    runner = _scripted_runner(_happy_script())
    result = _verify(backup_dir, pg_bin, runner)
    assert result["restored"] is False
    assert any("sha256 mismatch" in p for p in result["problems"])
    assert not any("createdb" in a[0] for a in runner.calls), "no DB work after a bad manifest"


def test_restore_blocked_when_manifest_missing(tmp_path, pg_bin):
    runner = _scripted_runner(_happy_script())
    result = _verify(tmp_path / "empty", pg_bin, runner)
    assert result["restored"] is False
    assert any("manifest not found" in p for p in result["problems"])


def test_restore_target_must_end_with_test(backup_dir, pg_bin):
    runner = _scripted_runner(_happy_script())
    result = _verify(backup_dir, pg_bin, runner, target_database="infraspine_ai")
    assert result["restored"] is False
    assert any("must end in _test" in p for p in result["problems"])
    assert not any("createdb" in a[0] for a in runner.calls)


def test_restore_target_must_differ_from_source(backup_dir, pg_bin):
    runner = _scripted_runner(_happy_script())
    result = _verify(backup_dir, pg_bin, runner, target_database="infraspine_ai_test")
    assert result["restored"] is False
    assert any("must differ from the source" in p for p in result["problems"])


def test_restore_rejects_unsafe_table_name(backup_dir, pg_bin):
    runner = _scripted_runner(_happy_script())
    result = _verify(backup_dir, pg_bin, runner, expected_tables=["users; DROP TABLE users"])
    assert result["restored"] is False
    assert any("Invalid expected table" in p for p in result["problems"])


def test_restore_head_mismatch_after_restore(backup_dir, pg_bin):
    script = [(lambda a: _is_psql_query(a, "alembic_version"), Completed(0, "0102\n", ""))]
    runner = _scripted_runner(script + _happy_script())
    result = _verify(backup_dir, pg_bin, runner)
    assert result["restored"] is False
    assert any("head mismatch" in p.lower() for p in result["problems"])
    assert any("dropdb" in a[0] for a in runner.calls), "throwaway DB dropped even on failure"


def test_restore_failure_still_drops_throwaway_db(backup_dir, pg_bin):
    script = [(lambda a: "psql" in a[0] and "-f" in a, Completed(1, "", "ERROR: syntax error"))]
    runner = _scripted_runner(script + _happy_script())
    result = _verify(backup_dir, pg_bin, runner)
    assert result["restored"] is False
    assert any("restore" in c["name"] and not c["ok"] for c in result["checks"])
    assert any("dropdb" in a[0] for a in runner.calls)


def test_createdb_failure_reports_without_restoring(backup_dir, pg_bin):
    script = [(lambda a: "createdb" in a[0], Completed(1, "", "createdb: database exists"))]
    runner = _scripted_runner(script + _happy_script())
    result = _verify(backup_dir, pg_bin, runner)
    assert result["restored"] is False
    assert not any("psql" in a[0] and "-f" in a for a in runner.calls)
    assert not any("dropdb" in a[0] for a in runner.calls), "nothing to drop when creation failed"


# --- Real PostgreSQL integration test ---------------------------------------

def _real_pg_bin():
    """Directory holding pg_dump/createdb/psql/dropdb, or None (test skips)."""
    from app.continuity.restore import _find_pg_tool

    for candidate in (os.environ.get("PG_BIN"), "/home/pgtest/.pg/usr/lib/postgresql/16/bin"):
        if not candidate:
            continue
        try:
            _find_pg_tool("pg_dump", candidate)
            return candidate
        except Exception:
            continue
    import shutil

    return None if shutil.which("pg_dump") is None else ""


@pytest.mark.slow
def test_real_backup_restores_into_throwaway_db(tmp_path):
    """End to end with real tools: dump the test DB, restore, verify, drop."""
    from tests.conftest import TEST_DATABASE_URL

    pg_bin = _real_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL client tools (pg_dump/psql) not available")
    from app.continuity.backup import current_alembic_head

    root = tmp_path / "backups"
    manifest = create_backup(backup_dir=root, database_url=TEST_DATABASE_URL,
                             alembic_head=current_alembic_head(), pg_bin=pg_bin or None)
    result = verify_restore(backup_dir=root, backup_id=manifest["backup_id"],
                            source_database_url=TEST_DATABASE_URL,
                            target_database="infraspine_stepg_restore_test",
                            pg_bin=pg_bin or None,
                            expected_tables=["users", "bc_tenants"])
    assert result["restored"] is True, result
    assert result["problems"] == []
    head = next(c for c in result["checks"] if c["name"] == "alembic_head")
    assert head["ok"] is True
