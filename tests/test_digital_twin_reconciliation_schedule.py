"""Scheduled drill-reconciliation cycle (Step K v2 slice 6, owed from slice 5).

DB-backed tests on a throwaway *_test database only, plus CLI
``--dry-run`` tests (pure comparison, no database). Proves the
cycle skips when there is no new drill run, runs and stores
findings once, skips an already-reconciled pair (fingerprint
gate), runs again for a new simulation, and re-evaluates a
matching pair (a match stores no findings, so the gate cannot
skip it — the re-run is idempotent and stores nothing).
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.digital_twin import reconciliation_schedule
from tests.test_digital_twin_reconciliation import _drill, _mismatch_kwargs, _simulation
from tests.test_digital_twin_reconciliation_pure import FP2, RUN, TENANT
from tests.test_digital_twin_versions import run

ROOT = Path(__file__).resolve().parent.parent


def _provider(bundle):
    async def provide():
        return bundle

    return provide


def _cycle(tid, bundle):
    async def go(sm):
        async with sm() as db:
            result = await reconciliation_schedule.run_reconciliation_cycle(
                db, tid, input_provider=_provider(bundle))
            await db.commit()
            return result

    return run(go)


def test_cycle_no_bundle_is_skipped():
    tid = uuid.uuid4()
    result = _cycle(tid, None)
    assert result["ran"] is False
    assert result["reason"] == "no_new_drill_run"


def test_cycle_runs_once_then_skips_already_reconciled():
    tid = uuid.uuid4()
    bundle = _mismatch_kwargs()
    first = _cycle(tid, bundle)
    assert first["ran"] is True
    assert first["reason"] == "evaluated"
    assert first["counts"]["total"] == 2
    assert len(first["recorded"]["created"]) == 2
    second = _cycle(tid, bundle)
    assert second["ran"] is False
    assert second["reason"] == "already_reconciled"
    assert second["drill_run_id"] == str(RUN)


def test_cycle_new_simulation_runs_again():
    tid = uuid.uuid4()
    _cycle(tid, _mismatch_kwargs())
    second = _cycle(tid, _mismatch_kwargs(fingerprint=FP2))
    assert second["ran"] is True
    assert len(second["recorded"]["created"]) == 2


def test_cycle_match_reruns_because_nothing_was_stored():
    tid = uuid.uuid4()
    bundle = {"simulation": _simulation(), "drill_observation": _drill(),
              "drill_run_id": str(RUN)}
    first = _cycle(tid, bundle)
    assert first["ran"] is True
    assert first["matches"] is True
    assert first["recorded"]["findings"] == []
    second = _cycle(tid, bundle)
    assert second["ran"] is True
    assert second["matches"] is True


def test_cycle_bad_bundle_and_provider():
    tid = uuid.uuid4()

    async def bad_bundle(sm):
        async with sm() as db:
            await reconciliation_schedule.run_reconciliation_cycle(
                db, tid, input_provider=_provider({"drill_observation": {}}))

    with pytest.raises(ValueError, match="needs a 'simulation' dict"):
        run(bad_bundle)

    async def bad_provider(sm):
        async with sm() as db:
            await reconciliation_schedule.run_reconciliation_cycle(
                db, tid, input_provider=None)

    with pytest.raises(ValueError, match="must be an async callable"):
        run(bad_provider)


def _write_json(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def test_cli_dry_run_reports_mismatch(tmp_path):
    sim_file = _write_json(tmp_path, "sim.json", _simulation(money={"PKR": 120000.0}))
    drill_file = _write_json(tmp_path, "drill.json", _drill(money={"PKR": 100000.0}))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "scripts/twin_reconcile.py",
         "--tenant", str(TENANT), "--drill-run-id", str(RUN),
         "--simulation", str(sim_file), "--drill-observation", str(drill_file),
         "--dry-run"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "1 finding(s)" in proc.stdout
    assert "money_mismatch" in proc.stdout


def test_cli_dry_run_match(tmp_path):
    sim_file = _write_json(tmp_path, "sim.json", _simulation())
    drill_file = _write_json(tmp_path, "drill.json", _drill())
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "scripts/twin_reconcile.py",
         "--tenant", str(TENANT), "--drill-run-id", str(RUN),
         "--simulation", str(sim_file), "--drill-observation", str(drill_file),
         "--dry-run"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "match" in proc.stdout


def test_cli_missing_file_exits_nonzero(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "scripts/twin_reconcile.py",
         "--tenant", str(TENANT), "--drill-run-id", str(RUN),
         "--simulation", str(tmp_path / "nope.json"),
         "--drill-observation", str(tmp_path / "nope2.json"), "--dry-run"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    assert proc.returncode != 0
