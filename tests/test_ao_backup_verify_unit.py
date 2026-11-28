"""Pure unit tests for Step AO backup-verification helpers. No database, no filesystem."""

from datetime import datetime, timedelta, timezone

import pytest

from app.ops.backup_verify import (
    EXPIRED,
    FRESH,
    STALE,
    BackupRecord,
    RestoreDrillResult,
    backup_freshness,
    is_valid_sha256,
    newest_verified,
    prune_plan,
    restore_drill_verdict,
    summarize_backups,
    validate_backup_record,
)

NOW = datetime(2026, 11, 28, 13, 0, tzinfo=timezone.utc)
GOOD_SHA = "a" * 64


def _record(**overrides):
    values = {
        "database": "infraspine",
        "path": "/backups/infraspine-20261128.dump",
        "bytes": 123456,
        "sha256": GOOD_SHA,
        "catalog_entries": 42,
        "verified": True,
        "created_at": NOW - timedelta(hours=2),
    }
    values.update(overrides)
    return BackupRecord(**values)


def test_valid_sha256_accepts_64_lowercase_hex():
    assert is_valid_sha256("0123456789abcdef" * 4) is True


def test_valid_sha256_rejects_uppercase_short_and_non_hex():
    assert is_valid_sha256("A" * 64) is False
    assert is_valid_sha256("a" * 63) is False
    assert is_valid_sha256("g" * 64) is False
    assert is_valid_sha256("") is False


def test_validate_good_record_has_no_problems():
    assert validate_backup_record(_record()) == []


def test_validate_names_each_bad_field():
    problems = validate_backup_record(
        _record(database="  ", path="", bytes=0, sha256="xyz", catalog_entries=0, verified=False)
    )
    assert "database is empty" in problems
    assert "path is empty" in problems
    assert "bytes must be positive" in problems
    assert "sha256 is not a 64-character lowercase hex digest" in problems
    assert "catalog_entries must be at least 1" in problems
    assert any("verified is false" in problem for problem in problems)


def test_freshness_bands_use_the_window():
    assert backup_freshness(NOW - timedelta(hours=24), NOW, 24) == FRESH
    assert backup_freshness(NOW - timedelta(hours=25), NOW, 24) == STALE
    assert backup_freshness(NOW - timedelta(hours=48), NOW, 24) == STALE
    assert backup_freshness(NOW - timedelta(hours=49), NOW, 24) == EXPIRED


def test_freshness_rejects_bad_window_and_future_backup():
    with pytest.raises(ValueError):
        backup_freshness(NOW, NOW, 0)
    assert backup_freshness(NOW + timedelta(hours=1), NOW, 24) == EXPIRED


def test_newest_verified_picks_newest_verified_not_newest_overall():
    old_verified = _record(created_at=NOW - timedelta(days=3))
    new_unverified = _record(created_at=NOW - timedelta(hours=1), verified=False)
    assert newest_verified([old_verified, new_unverified]) == old_verified
    assert newest_verified([new_unverified]) is None
    assert newest_verified([]) is None


def test_prune_keeps_newest_and_young_records():
    records = [
        _record(path=f"/b/{n}.dump", created_at=NOW - timedelta(days=n))
        for n in range(5)
    ]
    keep, prune = prune_plan(records, keep_newest=2, min_age_days=30, now=NOW)
    assert len(keep) == 5  # all younger than 30 days
    assert prune == []


def test_prune_drops_old_records_beyond_keep_newest():
    records = [
        _record(path=f"/b/{n}.dump", created_at=NOW - timedelta(days=n))
        for n in range(5)
    ]
    keep, prune = prune_plan(records, keep_newest=2, min_age_days=2, now=NOW)
    assert [record.path for record in keep] == ["/b/0.dump", "/b/1.dump"]
    assert [record.path for record in prune] == ["/b/2.dump", "/b/3.dump", "/b/4.dump"]


def test_prune_never_drops_the_newest_verified_backup():
    old_verified = _record(path="/b/old-verified.dump", created_at=NOW - timedelta(days=90))
    newer_unverified = [
        _record(path=f"/b/{n}.dump", created_at=NOW - timedelta(days=n), verified=False)
        for n in range(3)
    ]
    keep, prune = prune_plan(
        [old_verified, *newer_unverified], keep_newest=1, min_age_days=1, now=NOW
    )
    assert old_verified in keep
    assert old_verified not in prune


def test_prune_rejects_bad_arguments():
    with pytest.raises(ValueError):
        prune_plan([], keep_newest=0, min_age_days=1, now=NOW)
    with pytest.raises(ValueError):
        prune_plan([], keep_newest=1, min_age_days=-1, now=NOW)


def _drill(**overrides):
    values = {
        "restore_ok": True,
        "tables_expected": 10,
        "tables_restored": 10,
        "rows_checked": 100,
        "rows_matched": 100,
        "sha256_match": True,
    }
    values.update(overrides)
    return RestoreDrillResult(**values)


def test_drill_verdict_passes_when_everything_matches():
    verdict = restore_drill_verdict(_drill())
    assert verdict.passed is True
    assert verdict.failures == ()


def test_drill_verdict_names_each_failure():
    assert "restore failed" in restore_drill_verdict(_drill(restore_ok=False)).failures[0]
    verdict = restore_drill_verdict(_drill(tables_restored=9))
    assert verdict.passed is False
    assert any("table count mismatch" in failure for failure in verdict.failures)
    verdict = restore_drill_verdict(_drill(rows_matched=99))
    assert any("row mismatch" in failure for failure in verdict.failures)
    verdict = restore_drill_verdict(_drill(sha256_match=False))
    assert any("sha256 mismatch" in failure for failure in verdict.failures)


def test_drill_verdict_fails_when_nothing_was_compared():
    assert restore_drill_verdict(_drill(rows_checked=0)).passed is False
    assert restore_drill_verdict(_drill(tables_expected=0)).passed is False


def test_summarize_backups_counts_freshness_and_newest():
    records = [
        _record(path="/b/fresh.dump", created_at=NOW - timedelta(hours=2)),
        _record(path="/b/stale.dump", created_at=NOW - timedelta(hours=30)),
        _record(path="/b/old.dump", created_at=NOW - timedelta(days=5), verified=False),
    ]
    summary = summarize_backups(records, NOW, 24)
    assert summary["total"] == 3
    assert summary["verified"] == 2
    assert summary["fresh"] == 1
    assert summary["stale"] == 1
    assert summary["expired"] == 0
    assert summary["newest_verified_age_hours"] == 2
    assert summary["newest_verified"] == "/b/fresh.dump"


def test_summarize_empty_backup_set():
    summary = summarize_backups([], NOW, 24)
    assert summary["total"] == 0
    assert summary["verified"] == 0
    assert summary["newest_verified"] is None
    assert summary["newest_verified_age_hours"] is None
