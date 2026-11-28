"""Unit tests for Step AO slice 2: backup scheduler decisions (no database)."""

import hashlib
from pathlib import Path

import pytest

from app.ops.backup_schedule import (
    EMPTY_DATABASE,
    drill_database_name,
    drop_database,
    is_empty_catalog_error,
    sha256_file,
)
from app.ops.backup_verify import RestoreDrillResult, restore_drill_verdict


def test_drill_database_name_is_a_valid_identifier():
    # Stamps are lowercased: PostgreSQL folds unquoted identifiers to lowercase.
    assert drill_database_name("infraspine", "20261128T000000Z") == "ao_drill_infraspine_20261128t000000z"


def test_drill_database_name_sanitizes_and_caps_length():
    name = drill_database_name("My-App.DB", "20261128T000000Z")
    assert name.startswith("ao_drill_my_app_db_")
    assert len(name) <= 63
    long_name = drill_database_name("a" * 80, "b" * 80)
    assert len(long_name) <= 63


def test_drill_database_name_falls_back_for_an_unusable_source():
    # A source name with no usable characters sanitizes to the "db" fallback,
    # still a valid prefixed drill name — never a crash, never an unprefixed name.
    assert drill_database_name("!!!", "stamp") == "ao_drill_db_stamp"


def test_is_empty_catalog_error_matches_the_create_backup_refusal():
    assert is_empty_catalog_error(RuntimeError("The backup has no catalog entries."))
    assert not is_empty_catalog_error(RuntimeError("pg_dump failed (exit code 1)."))
    assert not is_empty_catalog_error(RuntimeError("connection refused"))


def test_sha256_file_matches_hashlib(tmp_path: Path):
    target = tmp_path / "backup.dump"
    target.write_bytes(b"infraspine-backup-bytes")
    assert sha256_file(target) == hashlib.sha256(b"infraspine-backup-bytes").hexdigest()


def test_zero_row_database_with_matching_tables_still_passes_its_drill():
    # A database with tables but legitimately zero rows proves its restore
    # when the restore completes, the table counts match and the digest matches.
    verdict = restore_drill_verdict(
        RestoreDrillResult(
            restore_ok=True,
            tables_expected=3,
            tables_restored=3,
            rows_checked=0,
            rows_matched=0,
            sha256_match=True,
        )
    )
    assert verdict.passed
    assert verdict.failures == ()


def test_drill_with_nothing_compared_at_all_still_fails():
    verdict = restore_drill_verdict(
        RestoreDrillResult(
            restore_ok=True,
            tables_expected=0,
            tables_restored=0,
            rows_checked=0,
            rows_matched=0,
            sha256_match=True,
        )
    )
    assert not verdict.passed
    assert any("nothing compared" in failure for failure in verdict.failures)


def test_drop_database_refuses_anything_but_a_drill_database():
    import asyncio

    with pytest.raises(ValueError, match="refusing to drop"):
        asyncio.run(drop_database("postgresql://localhost/postgres", "infraspine"))
    with pytest.raises(ValueError, match="refusing to drop"):
        asyncio.run(drop_database("postgresql://localhost/postgres", "ao_drill"))
