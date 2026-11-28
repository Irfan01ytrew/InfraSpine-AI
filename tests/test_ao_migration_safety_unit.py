"""Unit tests for Step AO slice 6: backup-before-migrate gate (no database)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.ops.migration_safety import backup_gate_decision


NOW = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
FRESH = NOW - timedelta(hours=2)
STALE = NOW - timedelta(days=3)
LAST_MIGRATION = NOW - timedelta(hours=5)


def test_fresh_verified_backup_newer_than_the_last_migration_allows():
    decision = backup_gate_decision([FRESH], LAST_MIGRATION, NOW)
    assert decision["allowed"] is True
    assert decision["reasons"] == []
    assert decision["newest_verified_backup"] == FRESH.isoformat()


def test_no_verified_backup_refuses():
    decision = backup_gate_decision([], LAST_MIGRATION, NOW)
    assert decision["allowed"] is False
    assert any("no verified backup" in reason for reason in decision["reasons"])


def test_backup_older_than_the_last_migration_refuses():
    decision = backup_gate_decision([NOW - timedelta(hours=8)], LAST_MIGRATION, NOW)
    assert decision["allowed"] is False
    assert any("predates the last migration" in reason for reason in decision["reasons"])


def test_backup_older_than_the_rpo_refuses_even_when_newer_than_the_migration():
    decision = backup_gate_decision([STALE], NOW - timedelta(days=5), NOW)
    assert decision["allowed"] is False
    assert any("older than the RPO" in reason for reason in decision["reasons"])


def test_unknown_last_migration_falls_back_to_freshness_and_says_so():
    decision = backup_gate_decision([FRESH], None, NOW)
    assert decision["allowed"] is True
    assert any("freshness alone" in reason for reason in decision["reasons"])
    stale_decision = backup_gate_decision([STALE], None, NOW)
    assert stale_decision["allowed"] is False


def test_only_the_newest_backup_counts():
    decision = backup_gate_decision([STALE, FRESH], LAST_MIGRATION, NOW)
    assert decision["allowed"] is True
