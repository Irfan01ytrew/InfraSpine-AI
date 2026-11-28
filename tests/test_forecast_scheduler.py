"""Tests for Step L slice 3 owed wiring: the forecast-scan scheduler + CLI.

Pure tests cover the scan report (fingerprint gating, quiet hours,
cooldown), snapshot/state file handling, and the summary. DB tests
run ``run_forecast_scan`` against throwaway ``*_test`` DBs (the
conftest migrates TEST_DATABASE_URL to head, including
0106_forecast_records). CLI tests invoke the real
``python -m app.cli forecast-scan`` entry point as a subprocess.
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.company_graph.models import GraphTenant
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.scheduler import (
    build_scan_report,
    format_scan_summary,
    load_snapshot,
    load_state,
    run_forecast_scan,
    save_state,
)
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("clean_tables")

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 28, 17, 0, tzinfo=PKT)  # daytime
QUIET_NOW = datetime(2026, 11, 28, 23, 0, tzinfo=PKT)  # quiet hours
# Fixture times are org time (Asia/Karachi): the watch reads the
# wall clock of ``now`` as org time and never converts, so a UTC
# datetime would misjudge quiet hours (23:00 PKT is 18:00 UTC).
ROOT = Path(__file__).resolve().parent.parent


def _snapshot():
    """Small fixture twin: Ayesha approves Pay suppliers (due 3 Dec),
    Bilal backs it up, ERP + Acme supply it, March batch is money."""
    entities = [
        {"id": "p-1", "kind": "person", "name": "Ayesha",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "p-2", "kind": "person", "name": "Bilal",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "t-1", "kind": "team", "name": "Finance", "attributes": {}},
        {"id": "sys-1", "kind": "system", "name": "ERP", "attributes": {}},
        {"id": "sup-1", "kind": "supplier", "name": "Acme Supplies", "attributes": {}},
        {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
         "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        {"id": "pr-2", "kind": "process", "name": "Onboard vendor",
         "attributes": {"due_at": "2026-12-20"}},
        {"id": "pr-3", "kind": "process", "name": "File tax return",
         "attributes": {"sla_days": 3}},
        {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
         "attributes": {"amount": 1500.00, "currency": "PKR",
                        "status": "awaiting_approval", "due_at": "2026-12-02"}},
        {"id": "wr-1", "kind": "workflow_run", "name": "Invoice run 7",
         "attributes": {"status": "running"}},
    ]
    relationships = [
        {"id": "e-1", "source_id": "p-1", "target_id": "pr-1", "kind": "approves", "confidence": 1.0},
        {"id": "e-2", "source_id": "p-1", "target_id": "pr-2", "kind": "owns", "confidence": 1.0},
        {"id": "e-3", "source_id": "p-1", "target_id": "pr-3", "kind": "responsible_for", "confidence": 1.0},
        {"id": "e-4", "source_id": "p-2", "target_id": "pr-1", "kind": "backup_for", "confidence": 1.0},
        {"id": "e-5", "source_id": "pr-1", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-6", "source_id": "sup-1", "target_id": "sys-1", "kind": "supplies", "confidence": 1.0},
        {"id": "e-7", "source_id": "b-1", "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-8", "source_id": "wr-1", "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-9", "source_id": "p-1", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
        {"id": "e-10", "source_id": "p-2", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
    ]
    return {"entities": entities, "relationships": relationships,
            "as_of": "2026-11-28T00:00:00+00:00"}


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def tenant():
    async def go(sm):
        async with sm() as db:
            row = GraphTenant(name="SchedCo")
            db.add(row)
            await db.commit()
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            return {"tenant_id": row.id, "admin_id": admin.id}

    return run(go)


# --- Pure scan report -------------------------------------------------------

def test_build_report_scans_every_critical_root_and_horizon():
    report = build_scan_report(_snapshot(), now=NOW)
    assert report["refreshed"] is True
    assert report["critical_ids"] == ["p-1", "sup-1", "sys-1"]
    assert report["horizons"] == [1, 7, 14, 30]
    assert report["counts"]["forecasts"] == 12
    assert report["counts"]["critical"] == 3
    assert len(report["forecasts"]) == 12
    assert report["alerts"]  # at least one rule fires in daytime
    assert all(line.startswith("[InfraSpine • predictive continuity •")
               for line in report["alerts"])
    assert report["sent_at"]  # fired alerts stamped for cooldown
    summary = format_scan_summary({
        **report, "recorded_new": 12, "recorded_existing": 0, "pruned": 0})
    assert "refreshed=True" in summary and "forecasts=12" in summary


def test_build_report_gated_by_previous_fingerprint():
    snapshot = _snapshot()
    first = build_scan_report(snapshot, now=NOW)
    second = build_scan_report(snapshot, now=NOW,
                               previous_fingerprint=first["fingerprint"],
                               sent_at=first["sent_at"])
    assert second["refreshed"] is False
    assert second["forecasts"] == []
    assert second["alerts"] == []
    assert second["fingerprint"] == first["fingerprint"]


def test_build_report_quiet_hours_defers_non_critical():
    report = build_scan_report(_snapshot(), now=QUIET_NOW)
    assert report["refreshed"] is True
    # Critical alerts break quiet hours; the rest are deferred.
    assert report["alerts"], "forecast_critical breaks quiet hours"
    assert report["deferred"], "non-breaking rules defer at night"
    assert all(entry["reason"] == "quiet_hours" for entry in report["deferred"])
    assert report["suppressed"] == []
    # The same instant as UTC 18:00 is daytime to the watch (it
    # reads the wall clock as org time, never converts), so nothing
    # defers and the warnings fire as alerts instead.
    daytime = build_scan_report(
        _snapshot(), now=datetime(2026, 11, 28, 18, 0, tzinfo=UTC))
    assert daytime["deferred"] == []
    assert len(daytime["alerts"]) > len(report["alerts"])


def test_build_report_cooldown_suppresses_refires():
    snapshot = _snapshot()
    first = build_scan_report(snapshot, now=NOW)
    assert first["alerts"]
    # Every fired alert is inside its cooldown: nothing fires again.
    second = build_scan_report(snapshot, now=NOW, sent_at=first["sent_at"],
                               previous_fingerprint=None)
    assert second["refreshed"] is True  # forced re-scan (no gating)
    assert second["alerts"] == []
    assert second["suppressed"]
    assert all(entry["reason"] == "cooldown" for entry in second["suppressed"])
    # Cooldowns expire: the same alerts fire again afterwards.
    later = build_scan_report(
        snapshot, now=NOW + timedelta(days=2), sent_at=first["sent_at"])
    assert later["alerts"]


def test_build_report_rejects_malformed_snapshot():
    with pytest.raises(ValueError):
        build_scan_report({"entities": "nope"}, now=NOW)
    with pytest.raises(ValueError):
        build_scan_report({"entities": [], "relationships": []}, now=NOW,
                          critical_ids=["ghost"])


# --- Snapshot / state files -------------------------------------------------

def test_load_snapshot_from_file_and_stdin(tmp_path, monkeypatch):
    path = tmp_path / "twin.json"
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    assert load_snapshot(path)["entities"][0]["id"] == "p-1"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_snapshot())))
    assert load_snapshot("-")["entities"][0]["id"] == "p-1"


def test_load_snapshot_rejects_bad_input(tmp_path):
    with pytest.raises(ValueError, match="not valid JSON"):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        load_snapshot(bad)
    with pytest.raises(ValueError, match="must be a JSON object"):
        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2]", encoding="utf-8")
        load_snapshot(arr)
    with pytest.raises(ValueError, match="Cannot read snapshot"):
        load_snapshot(tmp_path / "missing.json")


def test_state_roundtrip_and_malformed_state(tmp_path, capsys):
    path = tmp_path / "state.json"
    assert load_state(path) == {"fingerprint": None, "sent_at": {}}
    assert load_state(None) == {"fingerprint": None, "sent_at": {}}
    save_state(path, {"fingerprint": "abc", "sent_at": {"fp": NOW.isoformat()}})
    assert load_state(path) == {"fingerprint": "abc", "sent_at": {"fp": NOW.isoformat()}}
    path.write_text("{broken", encoding="utf-8")
    assert load_state(path) == {"fingerprint": None, "sent_at": {}}
    assert "malformed state file" in capsys.readouterr().err


# --- DB pass ----------------------------------------------------------------

def test_run_scan_records_and_is_idempotent(tenant):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            first = await run_forecast_scan(
                db, tenant["tenant_id"], snap, now=NOW,
                created_by=tenant["admin_id"])
            assert first["refreshed"] is True
            assert first["recorded_new"] == 12
            assert first["recorded_existing"] == 0
            assert len(first["alerts"]) > 0
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 12
            # Same twin again (state fingerprint): no writes at all.
            second = await run_forecast_scan(
                db, tenant["tenant_id"], snap, now=NOW,
                previous_fingerprint=first["fingerprint"],
                created_by=tenant["admin_id"])
            assert second["refreshed"] is False
            assert second["recorded_new"] == 0
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 12
            # Forced re-scan of the same twin: replays, never duplicates.
            third = await run_forecast_scan(
                db, tenant["tenant_id"], snap, now=NOW,
                created_by=tenant["admin_id"])
            assert third["refreshed"] is True
            assert third["recorded_new"] == 0
            assert third["recorded_existing"] == 12
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 12

    run(go)


def test_run_scan_unknown_tenant_or_creator_raises(tenant):
    async def go(sm):
        async with sm() as db:
            with pytest.raises(ValueError, match="Unknown tenant"):
                await run_forecast_scan(db, uuid.uuid4(), _snapshot(), now=NOW)
            with pytest.raises(ValueError, match="Unknown created_by"):
                await run_forecast_scan(db, tenant["tenant_id"], _snapshot(),
                                       now=NOW, created_by=uuid.uuid4())
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 0

    run(go)


def test_run_scan_prunes_aged_superseded(tenant):
    async def go(sm):
        async with sm() as db:
            snap1 = _snapshot()
            await run_forecast_scan(db, tenant["tenant_id"], snap1, now=NOW)
            snap2 = dict(snap1)
            snap2["entities"] = [e for e in snap1["entities"] if e["id"] != "pr-2"]
            snap2["relationships"] = [r for r in snap1["relationships"]
                                      if r["source_id"] != "pr-2" and r["target_id"] != "pr-2"]
            await run_forecast_scan(db, tenant["tenant_id"], snap2, now=NOW)
            rows = list(await db.scalars(select(ForecastRecord)))
            assert len(rows) == 24  # 12 current + 12 superseded
            # Age the superseded rows past retention, then scan a third
            # twin with pruning: only the aged rows are deleted.
            old_fps = {r.fingerprint for r in rows if r.superseded}
            assert len(old_fps) == 1
            await db.execute(update(ForecastRecord).where(
                ForecastRecord.superseded.is_(True)).values(
                created_at=NOW - timedelta(days=100)))
            await db.commit()
            snap3 = dict(snap2)
            snap3["entities"] = [e for e in snap2["entities"] if e["id"] != "pr-3"]
            snap3["relationships"] = [r for r in snap2["relationships"]
                                      if r["source_id"] != "pr-3" and r["target_id"] != "pr-3"]
            third = await run_forecast_scan(db, tenant["tenant_id"], snap3,
                                            now=NOW, prune=True)
            assert third["pruned"] == 12
            remaining = list(await db.scalars(select(ForecastRecord)))
            assert len(remaining) == 24  # 12 (scan3) + 12 (scan2, fresh superseded)

    run(go)


# --- CLI --------------------------------------------------------------------

def _cli(*argv, env=None):
    merged = dict(os.environ)
    merged["DATABASE_URL"] = TEST_DATABASE_URL
    merged["TEST_DATABASE_URL"] = TEST_DATABASE_URL
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=merged)


def test_cli_help_lists_forecast_scan():
    result = _cli("forecast-scan", "--help")
    assert result.returncode == 0
    assert "--tenant-id" in result.stdout and "--snapshot" in result.stdout
    assert "--dry-run" in result.stdout and "--loop" in result.stdout


def test_cli_dry_run_scans_without_writing(tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot()), encoding="utf-8")
    result = _cli("forecast-scan", "--tenant-id", str(uuid.uuid4()),
                  "--snapshot", str(snap), "--dry-run")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["refreshed"] is True
    assert payload["counts"]["forecasts"] == 12
    assert payload["alerts"]


def test_cli_usage_errors_exit_2(tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot()), encoding="utf-8")
    bad_id = _cli("forecast-scan", "--tenant-id", "not-a-uuid",
                  "--snapshot", str(snap), "--dry-run")
    assert bad_id.returncode == 2
    missing = _cli("forecast-scan", "--tenant-id", str(uuid.uuid4()),
                   "--snapshot", str(tmp_path / "nope.json"), "--dry-run")
    assert missing.returncode == 2
    both = _cli("forecast-scan", "--tenant-id", str(uuid.uuid4()),
                "--snapshot", str(snap), "--dry-run", "--loop")
    assert both.returncode == 2


def test_cli_real_run_records_and_second_run_is_unchanged(tenant, tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot()), encoding="utf-8")
    state = tmp_path / "state.json"
    args = ["forecast-scan", "--tenant-id", str(tenant["tenant_id"]),
            "--snapshot", str(snap), "--state-file", str(state),
            "--created-by", str(tenant["admin_id"])]
    first = _cli(*args)
    assert first.returncode == 0, first.stderr
    assert "refreshed=True" in first.stdout
    assert "recorded new=12" in first.stdout
    assert load_state(state)["fingerprint"]
    second = _cli(*args)
    assert second.returncode == 0, second.stderr
    assert "refreshed=False" in second.stdout
    assert "recorded new=0" in second.stdout

    async def check(sm):
        async with sm() as db:
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 12

    run(check)
