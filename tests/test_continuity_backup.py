"""Step G slice 2 — pg_dump backup + freshness check tests (database-free).

``create_backup`` runs a real ``pg_dump`` in production; here the runner
is faked (it writes fixed dump bytes), so no database is needed. The
real-binary path is covered by the restore integration test in
``tests/test_continuity_restore.py``.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.continuity.backup import (
    BackupError,
    Completed,
    check_backup,
    create_backup,
    current_alembic_head,
    find_pg_dump,
    latest_manifest,
    load_manifest,
)
from app.continuity.health import sha256_hex

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)
DUMP = b"PGDUMP-SYNTHETIC-BACKUP-BYTES"
URL = "postgresql://backup_user:secret-pass@127.0.0.1:5432/infraspine_ai_test"


@pytest.fixture
def pg_bin(tmp_path):
    """A directory with a dummy (never executed) pg_dump binary."""
    (tmp_path / "pg_dump").write_bytes(b"#!/bin/sh\nexit 0\n")
    (tmp_path / "pg_dump").chmod(0o755)
    return tmp_path


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


def _fake_runner(dump_bytes=DUMP, returncode=0, stderr=""):
    calls = []

    def run(argv, *, env=None):
        calls.append(argv)
        out = Path(argv[argv.index("-f") + 1])
        if returncode == 0:
            out.write_bytes(dump_bytes)
        return Completed(returncode, "", stderr)

    run.calls = calls
    return run


def _take(backup_dir, pg_bin, **overrides):
    kwargs = {"backup_dir": backup_dir, "database_url": URL, "alembic_head": "0103",
              "pg_bin": pg_bin, "now": NOW}
    kwargs.update(overrides)
    return create_backup(**kwargs)


def test_create_backup_writes_dump_and_manifest(backup_dir, pg_bin):
    runner = _fake_runner()
    manifest = _take(backup_dir, pg_bin, runner=runner)
    target = backup_dir / manifest["backup_id"]
    assert (target / "db.dump").read_bytes() == DUMP
    stored = json.loads((target / "manifest.json").read_text())
    assert stored == manifest
    assert manifest["backup_id"] == "bk-20261128-010000"
    assert manifest["kind"] == "full"
    assert manifest["alembic_head"] == "0103"
    assert manifest["database"] == "infraspine_ai_test"
    assert manifest["files"] == [{"path": "db.dump", "size_bytes": len(DUMP),
                                  "sha256": sha256_hex(DUMP)}]
    assert manifest["total_bytes"] == len(DUMP)
    # pg_dump got the URL and the dump path, nothing else interpolated.
    assert runner.calls[0][0].endswith("pg_dump")
    assert URL in runner.calls[0]


def test_create_backup_failure_never_leaks_url(backup_dir, pg_bin):
    runner = _fake_runner(returncode=1, stderr="FATAL: password authentication failed")
    with pytest.raises(BackupError) as exc:
        _take(backup_dir, pg_bin, runner=runner)
    assert "secret-pass" not in str(exc.value)
    assert "127.0.0.1" not in str(exc.value)
    assert "pg_dump failed" in str(exc.value)


def test_create_backup_rejects_traversal_backup_id(backup_dir, pg_bin):
    for bad in ("../escape", "bk-evil", "bk-20261128-010000/../../x", ""):
        with pytest.raises(BackupError):
            _take(backup_dir, pg_bin, runner=_fake_runner(), backup_id=bad)


def test_create_backup_rejects_duplicate_id(backup_dir, pg_bin):
    _take(backup_dir, pg_bin, runner=_fake_runner())
    with pytest.raises(BackupError, match="already exists"):
        _take(backup_dir, pg_bin, runner=_fake_runner())


def test_latest_manifest_picks_newest_and_ignores_broken(backup_dir, pg_bin):
    _take(backup_dir, pg_bin, runner=_fake_runner(), now=NOW - timedelta(hours=5))
    _take(backup_dir, pg_bin, runner=_fake_runner(), now=NOW - timedelta(hours=1))
    (backup_dir / "bk-20261128-003000").mkdir()
    (backup_dir / "bk-20261128-003000" / "manifest.json").write_text("{not json")
    found = latest_manifest(backup_dir)
    assert found is not None
    backup_id, manifest = found
    assert backup_id == "bk-20261128-000000"
    assert manifest["created_at"].startswith("2026-11-28T00:00")
    assert latest_manifest(backup_dir / "no-such-dir") is None
    assert load_manifest(backup_dir / "bk-20261128-003000") is None


def test_check_backup_fresh_when_intact(backup_dir, pg_bin):
    manifest = _take(backup_dir, pg_bin, runner=_fake_runner())
    report = check_backup(backup_dir, now=NOW + timedelta(hours=2))
    assert report["freshness"] == "fresh"
    assert report["backup_id"] == manifest["backup_id"]
    assert report["verification"]["valid"] is True
    assert report["verification"]["problems"] == []


def test_check_backup_stale_when_old(backup_dir, pg_bin):
    _take(backup_dir, pg_bin, runner=_fake_runner(), now=NOW - timedelta(hours=30))
    report = check_backup(backup_dir, now=NOW)
    assert report["freshness"] == "stale"
    assert report["verification"]["valid"] is False
    assert any("stale" in p for p in report["verification"]["problems"])


def test_check_backup_tampered_dump_fails_verification(backup_dir, pg_bin):
    manifest = _take(backup_dir, pg_bin, runner=_fake_runner())
    (backup_dir / manifest["backup_id"] / "db.dump").write_bytes(b"tampered")
    report = check_backup(backup_dir, now=NOW + timedelta(hours=1))
    assert report["verification"]["valid"] is False
    assert any("sha256 mismatch" in p for p in report["verification"]["problems"])


def test_check_backup_missing_dump_fails_verification(backup_dir, pg_bin):
    manifest = _take(backup_dir, pg_bin, runner=_fake_runner())
    (backup_dir / manifest["backup_id"] / "db.dump").unlink()
    report = check_backup(backup_dir, now=NOW + timedelta(hours=1))
    assert report["verification"]["valid"] is False
    assert any("missing file" in p for p in report["verification"]["problems"])


def test_check_backup_head_mismatch_is_reported(backup_dir, pg_bin):
    _take(backup_dir, pg_bin, runner=_fake_runner())
    report = check_backup(backup_dir, now=NOW + timedelta(hours=1), expected_alembic_head="9999")
    assert report["verification"]["valid"] is False
    assert any("alembic head mismatch" in p for p in report["verification"]["problems"])


def test_check_backup_missing_directory(backup_dir):
    report = check_backup(backup_dir, now=NOW)
    assert report["freshness"] == "missing"
    assert report["backup_id"] is None
    assert report["verification"]["valid"] is False


def test_find_pg_dump_requires_a_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(BackupError, match="pg_dump not found"):
        find_pg_dump()


def test_current_alembic_head_is_the_branch_head():
    assert current_alembic_head() == "0113_continuity_unified_origin"


def test_libpq_url_strips_the_asyncpg_driver():
    from app.continuity.backup import libpq_url

    assert libpq_url("postgresql+asyncpg://user:pass@127.0.0.1:55441/infraspine_stepg_test") == (
        "postgresql://user:pass@127.0.0.1:55441/infraspine_stepg_test")
    assert libpq_url("postgresql://user@127.0.0.1/db") == "postgresql://user@127.0.0.1/db"
