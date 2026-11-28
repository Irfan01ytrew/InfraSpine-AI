"""Tests for Step L slice 4: AG leave wired into calendar forecasting.

Pure tests cover the AG-row mapping (real AG field shapes, all four
CHECK statuses, applied marking, half days, malformed rows). DB
tests run the loader + end-to-end path against throwaway ``*_test``
DBs: the ``hr_leave_requests`` table is created from the REAL AG
model DDL (loaded from the sibling ``agent/hr-module`` worktree —
skipped when that worktree is absent), rows are inserted through
the real AG model (so Postgres CHECK constraints enforce the
status set), and the loader reads them back. CLI tests invoke the
real ``python -m app.cli forecast-calendar`` entry point.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import AccessGroup, User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity.ag_leave import (
    AG_LEAVE_STATUSES,
    AG_LEAVE_TABLE,
    AG_PLANNED_STATUSES,
    AgLeaveUnavailable,
    calendar_forecasts_from_ag,
    load_ag_leave_records,
    map_ag_leave_request,
    map_ag_leave_requests,
)
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, run_db

pytestmark = pytest.mark.usefixtures("clean_tables")

ROOT = Path(__file__).resolve().parent.parent
AG_MODELS_PATH = Path(
    "/home/hatch/workspace/infraspine/work/hr-module/app/hr/models.py")
TODAY = date(2026, 11, 28)


def _ag_row(**overrides):
    row = {
        "employee_id": uuid.uuid4(),
        "start_date": date(2026, 12, 5),
        "end_date": date(2026, 12, 12),
        "status": "approved",
        "leave_type": "annual",
        "half_day": False,
        "applied_at": None,
    }
    row.update(overrides)
    return row


# --- pure mapping ------------------------------------------------------------

def test_map_approved_row():
    mapped = map_ag_leave_request(_ag_row())
    assert mapped["status"] == "approved"
    assert mapped["start"] == date(2026, 12, 5)
    assert mapped["end"] == date(2026, 12, 12)
    assert mapped["leave_type"] == "annual"
    assert mapped["half_day"] is False
    assert mapped["applied"] is False


def test_map_applied_approved_row_keeps_status_approved():
    # AG has NO 'applied' status: execution keeps status 'approved'
    # and is marked by applied_at. The planning value is the same.
    mapped = map_ag_leave_request(
        _ag_row(applied_at=datetime(2026, 11, 20, 9, 0)))
    assert mapped["status"] == "approved"
    assert mapped["applied"] is True


def test_map_applied_status_is_unknown():
    # 'applied' is not in AG's CHECK set — a row claiming it is a
    # schema change, not a leave state.
    with pytest.raises(ValueError, match="Unknown AG leave status 'applied'"):
        map_ag_leave_request(_ag_row(status="applied"))


def test_map_non_planned_statuses_are_skipped():
    for status in ("pending", "rejected", "cancelled"):
        assert map_ag_leave_request(_ag_row(status=status)) is None


def test_map_half_day_maps_to_whole_day():
    # Calendar math works in whole days; a half-day absence is
    # conservatively treated as a full-day absence (advisory-safe).
    mapped = map_ag_leave_request(_ag_row(half_day=True))
    assert mapped["half_day"] is True
    assert mapped["start"] == date(2026, 12, 5)
    assert mapped["end"] == date(2026, 12, 12)


def test_map_accepts_iso_strings_and_datetimes():
    mapped = map_ag_leave_request(_ag_row(
        start_date="2026-12-05", end_date=datetime(2026, 12, 12, 18, 30)))
    assert mapped["start"] == date(2026, 12, 5)
    assert mapped["end"] == date(2026, 12, 12)


def test_map_accepts_orm_style_rows():
    class Row:
        employee_id = uuid.uuid4()
        start_date = date(2026, 12, 5)
        end_date = date(2026, 12, 7)
        status = "approved"
        half_day = False
        leave_type = "sick"
        applied_at = None

    mapped = map_ag_leave_request(Row())
    assert mapped["person_id"] == str(Row.employee_id)
    assert mapped["leave_type"] == "sick"


def test_map_rejects_malformed_rows():
    with pytest.raises(ValueError, match="employee_id"):
        map_ag_leave_request(_ag_row(employee_id=None))
    with pytest.raises(ValueError, match="status"):
        map_ag_leave_request(_ag_row(status=None))
    with pytest.raises(ValueError, match="before it starts"):
        map_ag_leave_request(_ag_row(start_date=date(2026, 12, 12),
                                     end_date=date(2026, 12, 5)))
    with pytest.raises(ValueError, match="over the 365-day cap"):
        map_ag_leave_request(_ag_row(start_date=date(2026, 1, 1),
                                     end_date=date(2027, 6, 1)))


def test_map_many_sorts_and_skips():
    rows = map_ag_leave_requests([
        _ag_row(start_date=date(2026, 12, 10), end_date=date(2026, 12, 11)),
        _ag_row(status="pending"),
        _ag_row(start_date=date(2026, 12, 5), end_date=date(2026, 12, 5)),
    ])
    assert [(r["start"], r["end"]) for r in rows] == [
        (date(2026, 12, 5), date(2026, 12, 5)),
        (date(2026, 12, 10), date(2026, 12, 11)),
    ]
    assert AG_LEAVE_STATUSES == {"pending", "approved", "rejected", "cancelled"}
    assert AG_PLANNED_STATUSES == {"approved"}


# --- DB helpers ---------------------------------------------------------------

def _load_ag_models():
    """The real AG model module, loaded from the sibling worktree."""
    if not AG_MODELS_PATH.is_file():
        pytest.skip("AG worktree not present — DB tests need agent/hr-module")
    spec = importlib.util.spec_from_file_location(
        "ag_hr_models_slice4", AG_MODELS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool,
                                     **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(scope="module")
def ag_table():
    """Create hr_leave_requests from the REAL AG model DDL."""
    models = _load_ag_models()
    table = models.HrLeaveRequest.__table__

    async def _create(conn):
        await conn.run_sync(lambda s: table.create(s, checkfirst=True))

    run_db(_create)
    return models


def _seed_group_and_users():
    async def go(sm):
        async with sm() as db:
            group = AccessGroup(name="L-Dept", kind="department", source="local")
            db.add(group)
            await db.flush()
            user = await db.scalar(select(User).where(User.email == USER_EMAIL))
            user2 = await db.scalar(select(User).where(User.email == USER2_EMAIL))
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            # Commit: the group row must exist for the AG table's
            # group_id FK — a flush alone is rolled back on dispose.
            await db.commit()
            return {"group_id": group.id, "u1": user.id, "u2": user2.id,
                    "admin": admin.id}

    return run(go)


def _snapshot(u1, u2):
    """Ayesha (u1) approves Pay suppliers (due 3 Dec), Bilal (u2)
    backs it up; Onboard vendor is due 20 Dec and owned by u1."""
    return {
        "entities": [
            {"id": str(u1), "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": str(u2), "kind": "person", "name": "Bilal",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
             "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
            {"id": "pr-2", "kind": "process", "name": "Onboard vendor",
             "attributes": {"due_at": "2026-12-20"}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": str(u1), "target_id": "pr-1",
             "kind": "approves", "confidence": 1.0},
            {"id": "e-2", "source_id": str(u1), "target_id": "pr-2",
             "kind": "owns", "confidence": 1.0},
            {"id": "e-3", "source_id": str(u2), "target_id": "pr-1",
             "kind": "backup_for", "confidence": 1.0},
        ],
    }


# --- DB: loader ----------------------------------------------------------------

class TestAgLeaveLoader:
    def test_loader_reads_real_ag_rows(self, ag_table):
        seed = _seed_group_and_users()
        models = ag_table

        async def go(sm):
            async with sm() as db:
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u1"],
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 12), days=Decimal("8"),
                    status="approved"))
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u2"],
                    leave_type="sick", start_date=date(2026, 12, 2),
                    end_date=date(2026, 12, 3), days=Decimal("2"),
                    status="pending"))
                await db.commit()
                return await load_ag_leave_records(db, seed["group_id"])

        records = run(go)
        assert len(records) == 1  # pending is skipped
        assert records[0]["person_id"] == str(seed["u1"])
        assert records[0]["start"] == date(2026, 12, 5)
        assert records[0]["end"] == date(2026, 12, 12)
        assert records[0]["leave_type"] == "annual"

    def test_loader_scopes_to_group_and_window(self, ag_table):
        seed = _seed_group_and_users()
        models = ag_table

        async def go(sm):
            async with sm() as db:
                other = AccessGroup(name="Other", kind="department", source="local")
                db.add(other)
                await db.flush()
                db.add(models.HrLeaveRequest(
                    group_id=other.id, employee_id=seed["u1"],
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 12), days=Decimal("8"),
                    status="approved"))
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u1"],
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 12), days=Decimal("8"),
                    status="approved"))
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u1"],
                    leave_type="casual", start_date=date(2026, 10, 1),
                    end_date=date(2026, 10, 2), days=Decimal("2"),
                    status="approved"))
                await db.commit()
                all_rows = await load_ag_leave_records(db, seed["group_id"])
                windowed = await load_ag_leave_records(
                    db, seed["group_id"], start=date(2026, 12, 1),
                    end=date(2026, 12, 31))
                return all_rows, windowed

        all_rows, windowed = run(go)
        assert len(all_rows) == 2  # other group's row excluded
        assert len(windowed) == 1  # October leave outside the window
        assert windowed[0]["leave_type"] == "annual"

    def test_loader_rejects_bad_group_and_window(self, ag_table):
        async def go(sm):
            async with sm() as db:
                with pytest.raises(ValueError, match="group_id must be a UUID"):
                    await load_ag_leave_records(db, "not-a-uuid")
                with pytest.raises(ValueError, match="ends .* before it starts"):
                    await load_ag_leave_records(
                        db, uuid.uuid4(), start=date(2026, 12, 10),
                        end=date(2026, 12, 1))

        run(go)

    def test_loader_missing_table_is_unavailable(self, ag_table, monkeypatch):
        import app.predictive_continuity.ag_leave as ag_leave

        monkeypatch.setattr(ag_leave, "AG_LEAVE_TABLE", "hr_leave_requests_missing")

        async def go(sm):
            async with sm() as db:
                with pytest.raises(AgLeaveUnavailable, match="is missing"):
                    await load_ag_leave_records(db, uuid.uuid4())

        run(go)

    def test_loader_columns_match_ag_model(self, ag_table):
        import app.predictive_continuity.ag_leave as ag_leave

        table = ag_table.HrLeaveRequest.__table__
        assert table.name == AG_LEAVE_TABLE
        assert ag_leave._READ_COLUMNS <= set(table.columns.keys())
        assert set(ag_table.LEAVE_STATUSES) == AG_LEAVE_STATUSES

    def test_ag_check_rejects_applied_status(self, ag_table):
        # Proves 'applied' is not an AG status: Postgres enforces it.
        seed = _seed_group_and_users()
        models = ag_table

        async def go(sm):
            async with sm() as db:
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u1"],
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 6), days=Decimal("2"),
                    status="applied"))
                with pytest.raises(Exception):
                    await db.commit()
                await db.rollback()

        run(go)


# --- DB: end to end ------------------------------------------------------------

class TestCalendarForecastsFromAg:
    def test_end_to_end_window_forecasts_and_warnings(self, ag_table):
        seed = _seed_group_and_users()
        models = ag_table
        snapshot = _snapshot(seed["u1"], seed["u2"])

        async def go(sm):
            async with sm() as db:
                # u1 (primary approver) on leave 5-12 Dec; u2 (backup)
                # on leave 1-4 Dec, overlapping the Pay suppliers window.
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u1"],
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 12), days=Decimal("8"),
                    status="approved"))
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=seed["u2"],
                    leave_type="sick", start_date=date(2026, 12, 1),
                    end_date=date(2026, 12, 4), days=Decimal("4"),
                    status="approved"))
                await db.commit()
                return await calendar_forecasts_from_ag(
                    snapshot, db, seed["group_id"], now=datetime(2026, 11, 28))

        result = run(go)
        assert result["group_id"] == str(seed["group_id"])
        assert len(result["leave_records"]) == 2
        assert result["leave_records"][0]["start"] == "2026-12-01"
        # One window forecast per leave: horizon = leave length.
        assert len(result["window_forecasts"]) == 2
        first = result["window_forecasts"][0]
        assert first["days"] == 4  # 1-4 Dec
        assert first["forecast"]["unavailable"][0]["id"] == str(seed["u2"])
        # Backup u2 is on leave while Pay suppliers is due: warning.
        warnings = result["backup_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["process_id"] == "pr-1"
        assert warnings[0]["backup_person_id"] == str(seed["u2"])
        assert warnings[0]["severity"] == "warning"  # u1 still covers it
        assert warnings[0]["lead_days"] == 3  # overlap starts 1 Dec

    def test_end_to_end_critical_when_every_primary_absent(self, ag_table):
        seed = _seed_group_and_users()
        models = ag_table
        snapshot = _snapshot(seed["u1"], seed["u2"])

        async def go(sm):
            async with sm() as db:
                # BOTH u1 (primary) and u2 (backup) on leave 1-4 Dec:
                # nobody is left to cover Pay suppliers.
                for user_id in (seed["u1"], seed["u2"]):
                    db.add(models.HrLeaveRequest(
                        group_id=seed["group_id"], employee_id=user_id,
                        leave_type="annual", start_date=date(2026, 12, 1),
                        end_date=date(2026, 12, 4), days=Decimal("4"),
                        status="approved"))
                await db.commit()
                return await calendar_forecasts_from_ag(
                    snapshot, db, seed["group_id"], now=datetime(2026, 11, 28))

        result = run(go)
        assert len(result["backup_warnings"]) == 1
        assert result["backup_warnings"][0]["severity"] == "critical"

    def test_end_to_end_unknown_person_raises(self, ag_table):
        seed = _seed_group_and_users()
        models = ag_table
        snapshot = _snapshot(seed["u1"], seed["u2"])
        # The admin is a real user (the AG employee_id FK requires
        # one) but NOT a person in the snapshot: leave for someone
        # the snapshot does not know must still raise.
        stranger = seed["admin"]

        async def go(sm):
            async with sm() as db:
                db.add(models.HrLeaveRequest(
                    group_id=seed["group_id"], employee_id=stranger,
                    leave_type="annual", start_date=date(2026, 12, 5),
                    end_date=date(2026, 12, 6), days=Decimal("2"),
                    status="approved"))
                await db.commit()
                with pytest.raises(ValueError, match="unknown person"):
                    await calendar_forecasts_from_ag(
                        snapshot, db, seed["group_id"],
                        now=datetime(2026, 11, 28))

        run(go)


# --- CLI -----------------------------------------------------------------------

def _cli(*argv, env=None):
    merged = dict(os.environ)
    merged["DATABASE_URL"] = TEST_DATABASE_URL
    merged["TEST_DATABASE_URL"] = TEST_DATABASE_URL
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=merged)


def test_cli_help_lists_forecast_calendar():
    result = _cli("forecast-calendar", "--help")
    assert result.returncode == 0
    assert "--group-id" in result.stdout and "--snapshot" in result.stdout
    assert "--window-days" in result.stdout


def test_cli_usage_errors_exit_2(tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot(uuid.uuid4(), uuid.uuid4())),
                    encoding="utf-8")
    bad_id = _cli("forecast-calendar", "--group-id", "not-a-uuid",
                  "--snapshot", str(snap))
    assert bad_id.returncode == 2
    missing = _cli("forecast-calendar", "--group-id", str(uuid.uuid4()),
                   "--snapshot", str(tmp_path / "nope.json"))
    assert missing.returncode == 2


def test_cli_real_run_prints_calendar_forecasts(ag_table, tmp_path):
    seed = _seed_group_and_users()
    models = ag_table

    async def go(sm):
        async with sm() as db:
            db.add(models.HrLeaveRequest(
                group_id=seed["group_id"], employee_id=seed["u1"],
                leave_type="annual", start_date=date(2026, 12, 5),
                end_date=date(2026, 12, 12), days=Decimal("8"),
                status="approved"))
            await db.commit()

    run(go)
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot(seed["u1"], seed["u2"])),
                    encoding="utf-8")
    result = _cli("forecast-calendar", "--group-id", str(seed["group_id"]),
                  "--snapshot", str(snap))
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["group_id"] == str(seed["group_id"])
    assert len(out["leave_records"]) == 1
    assert len(out["window_forecasts"]) == 1
    assert out["window_forecasts"][0]["days"] == 8
    assert out["backup_warnings"] == []


def test_cli_unknown_department_exit_2(ag_table, tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_snapshot(uuid.uuid4(), uuid.uuid4())),
                    encoding="utf-8")
    result = _cli("forecast-calendar", "--group-id", str(uuid.uuid4()),
                  "--snapshot", str(snap))
    assert result.returncode == 2
    assert "Unknown department" in result.stderr
