"""Step G slice 1 — health roll-up + backup manifest tests (database-free).

Pure-function tests: no DB, no network, no live data. They run in a minimal
pytest venv with ``--noconftest`` and also under the full suite (this file
imports only the standard library and ``app.continuity.health``).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.continuity.health import (
    aggregate_health,
    backup_freshness,
    build_backup_manifest,
    evaluate_probe,
    sha256_hex,
    summarize_health,
    verify_backup_manifest,
)

NOW = datetime(2026, 11, 28, 0, 30, tzinfo=timezone.utc)
DATA = b"infraspine-backup-bytes"
DIGEST = sha256_hex(DATA)


def _manifest(**overrides):
    base = build_backup_manifest(
        backup_id="bk-20261128-0030",
        created_at=NOW - timedelta(hours=2),
        alembic_head="0103",
        database="infraspine_ai",
        files=[{"path": "db.dump", "size_bytes": len(DATA), "sha256": DIGEST}],
    )
    base.update(overrides)
    return base


def _available(**overrides):
    files = {"db.dump": (len(DATA), DIGEST)}
    files.update(overrides)
    return files


# --- evaluate_probe -------------------------------------------------------

def test_evaluate_probe_accepts_known_component():
    probe = evaluate_probe("api", "ok", NOW, detail="200 in 12ms", latency_ms=12)
    assert probe.component == "api"
    assert probe.status == "ok"
    assert probe.latency_ms == 12


def test_evaluate_probe_rejects_unknown_component():
    with pytest.raises(ValueError, match="Unknown continuity component"):
        evaluate_probe("toaster", "ok", NOW)


def test_evaluate_probe_rejects_invalid_status_and_negative_latency():
    with pytest.raises(ValueError, match="Invalid probe status"):
        evaluate_probe("api", "fine", NOW)
    with pytest.raises(ValueError, match="latency_ms"):
        evaluate_probe("api", "ok", NOW, latency_ms=-1)


# --- aggregate_health -----------------------------------------------------

def test_aggregate_all_ok_is_ok():
    probes = [evaluate_probe(c, "ok", NOW) for c in
              ("api", "database", "workflow_worker", "ingestion_worker",
               "vector_index", "backup")]
    result = aggregate_health(probes, now=NOW)
    assert result["status"] == "ok"
    assert result["components_ok"] == 6
    assert result["components_total"] == 6


def test_aggregate_one_down_is_down():
    probes = [
        evaluate_probe("api", "ok", NOW),
        evaluate_probe("database", "down", NOW, detail="connection refused"),
    ]
    result = aggregate_health(probes, now=NOW)
    assert result["status"] == "down"
    assert result["components"]["database"]["status"] == "down"


def test_aggregate_degraded_beats_ok_but_not_down():
    probes = [
        evaluate_probe("api", "ok", NOW),
        evaluate_probe("backup", "degraded", NOW, detail="last backup 20h ago"),
    ]
    assert aggregate_health(probes, now=NOW)["status"] == "degraded"


def test_aggregate_stale_probe_counts_as_unknown():
    probes = [evaluate_probe("api", "ok", NOW - timedelta(minutes=30))]
    result = aggregate_health(probes, now=NOW)
    assert result["components"]["api"]["status"] == "unknown"
    assert result["components"]["api"]["stale"] is True
    assert result["components"]["api"]["recorded_status"] == "ok"
    # Overall is "unknown", not "degraded": the five unprobed components
    # also count as unknown, and worst-status-wins applies.
    assert result["status"] == "unknown"


def test_aggregate_missing_component_and_empty_list_are_unknown():
    result = aggregate_health([], now=NOW)
    assert result["status"] == "unknown"
    assert result["components"]["database"]["status"] == "unknown"
    assert result["components_ok"] == 0


# --- build_backup_manifest ------------------------------------------------

def test_build_manifest_computes_counts_and_totals():
    manifest = _manifest()
    assert manifest["file_count"] == 1
    assert manifest["total_bytes"] == len(DATA)
    assert manifest["files"][0]["sha256"] == DIGEST


def test_build_manifest_rejects_duplicate_path_bad_hash_and_empty_files():
    with pytest.raises(ValueError, match="Duplicate backup file path"):
        build_backup_manifest(
            backup_id="bk-x", created_at=NOW, alembic_head="0103",
            database="db",
            files=[
                {"path": "a.dump", "size_bytes": 1, "sha256": DIGEST},
                {"path": "a.dump", "size_bytes": 1, "sha256": DIGEST},
            ],
        )
    with pytest.raises(ValueError, match="Invalid sha256"):
        build_backup_manifest(
            backup_id="bk-x", created_at=NOW, alembic_head="0103",
            database="db",
            files=[{"path": "a.dump", "size_bytes": 1, "sha256": "not-a-hash"}],
        )
    with pytest.raises(ValueError, match="at least one file"):
        build_backup_manifest(
            backup_id="bk-x", created_at=NOW, alembic_head="0103",
            database="db", files=[],
        )


# --- verify_backup_manifest -----------------------------------------------

def test_verify_valid_manifest_passes():
    verdict = verify_backup_manifest(
        _manifest(), available_files=_available(), now=NOW,
        expected_alembic_head="0103",
    )
    assert verdict == {"valid": True, "problems": [], "checked": 1}


def test_verify_detects_missing_file_size_and_hash_mismatch():
    missing = verify_backup_manifest(_manifest(), available_files={}, now=NOW)
    assert missing["valid"] is False
    assert any("missing file" in p for p in missing["problems"])

    wrong_size = verify_backup_manifest(
        _manifest(), available_files={"db.dump": (999, DIGEST)}, now=NOW)
    assert wrong_size["valid"] is False
    assert any("size mismatch" in p for p in wrong_size["problems"])

    wrong_hash = verify_backup_manifest(
        _manifest(),
        available_files={"db.dump": (len(DATA), "0" * 64)},
        now=NOW,
    )
    assert wrong_hash["valid"] is False
    assert any("sha256 mismatch" in p for p in wrong_hash["problems"])


def test_verify_detects_stale_backup_and_wrong_alembic_head():
    old = _manifest(created_at=NOW - timedelta(hours=30))
    stale = verify_backup_manifest(old, available_files=_available(), now=NOW)
    assert stale["valid"] is False
    assert any("stale" in p for p in stale["problems"])

    wrong_head = verify_backup_manifest(
        _manifest(), available_files=_available(), now=NOW,
        expected_alembic_head="0102",
    )
    assert wrong_head["valid"] is False
    assert any("alembic head mismatch" in p for p in wrong_head["problems"])


def test_verify_detects_empty_and_duplicate_manifest():
    empty = verify_backup_manifest(
        {"backup_id": "bk-empty", "files": [], "created_at": NOW.isoformat()},
        available_files={}, now=NOW,
    )
    assert empty["valid"] is False
    assert any("no files" in p for p in empty["problems"])

    dup = _manifest(files=[
        {"path": "db.dump", "size_bytes": len(DATA), "sha256": DIGEST},
        {"path": "db.dump", "size_bytes": len(DATA), "sha256": DIGEST},
    ])
    dup_verdict = verify_backup_manifest(dup, available_files=_available(), now=NOW)
    assert dup_verdict["valid"] is False
    assert any("duplicate" in p for p in dup_verdict["problems"])


# --- freshness + summary ----------------------------------------------------

def test_backup_freshness_states():
    assert backup_freshness(None, now=NOW) == "missing"
    assert backup_freshness(_manifest(), now=NOW) == "fresh"
    old = _manifest(created_at=NOW - timedelta(hours=30))
    assert backup_freshness(old, now=NOW) == "stale"
    broken = _manifest(created_at="not-a-date")
    assert backup_freshness(broken, now=NOW) == "stale"


def test_summarize_health_ready_only_when_ok_and_fresh():
    probes = [evaluate_probe(c, "ok", NOW) for c in
              ("api", "database", "workflow_worker", "ingestion_worker",
               "vector_index", "backup")]
    summary = summarize_health(aggregate_health(probes, now=NOW), _manifest(), now=NOW)
    assert summary["continuity_ready"] is True
    assert summary["status"] == "ok"
    assert summary["backup"] == "fresh"

    degraded = summarize_health(aggregate_health(probes, now=NOW), None, now=NOW)
    assert degraded["continuity_ready"] is False
    assert degraded["backup"] == "missing"
    assert "No backup manifest" in degraded["summary"]


def test_summarize_health_down_blocks_readiness_even_with_fresh_backup():
    probes = [evaluate_probe("database", "down", NOW, detail="unreachable")]
    summary = summarize_health(aggregate_health(probes, now=NOW), _manifest(), now=NOW)
    assert summary["status"] == "down"
    assert summary["continuity_ready"] is False


def test_sha256_hex_matches_known_digest():
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_hex(DATA) == DIGEST
