"""Tests for Step L slice 3: forecast records + visibility filtering.

Runs against real PostgreSQL (the conftest migrates TEST_DATABASE_URL
to head, including 0120_forecast_records) and the pure filter
functions. The pure filter tests need no database; the service tests
use the ``run`` helper (async session on TEST_DATABASE_URL).

Setup per test: a graph tenant with an owner (ADMIN_EMAIL), a viewer
(USER_EMAIL) holding ``forecasts.read``, and a second viewer
(USER2_EMAIL) who is a graph member but holds no ``forecasts.read``
(403). Two snapshot entities are restricted (``pr-2``, ``b-1``) so
the viewer must not see them anywhere — not in lists, counts, or
money totals — while the owner sees the full forecast.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.company_graph.models import GraphMember, GraphRecord, GraphTenant
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity.forecast import forecast_unavailability
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.records import (
    filter_forecast_visibility,
    get_forecast,
    list_forecasts,
    prune_forecasts,
    record_scan,
)
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
)

pytestmark = pytest.mark.usefixtures("clean_tables")

NOW = datetime(2026, 11, 28, tzinfo=UTC)
IDS = {key: str(uuid.uuid4()) for key in
       ("p-1", "p-2", "t-1", "sys-1", "sup-1", "pr-1", "pr-2", "pr-3", "b-1", "wr-1")}


def _snapshot(remove=()):
    """Slice-1 fixture snapshot with UUID ids; pr-2 and b-1 restricted."""
    entities = [
        {"id": IDS["p-1"], "kind": "person", "name": "Ayesha",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": IDS["p-2"], "kind": "person", "name": "Bilal",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": IDS["t-1"], "kind": "team", "name": "Finance", "attributes": {}},
        {"id": IDS["sys-1"], "kind": "system", "name": "ERP", "attributes": {}},
        {"id": IDS["sup-1"], "kind": "supplier", "name": "Acme Supplies", "attributes": {}},
        {"id": IDS["pr-1"], "kind": "process", "name": "Pay suppliers",
         "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        {"id": IDS["pr-2"], "kind": "process", "name": "Onboard vendor",
         "attributes": {"due_at": "2026-12-20"}},
        {"id": IDS["pr-3"], "kind": "process", "name": "File tax return",
         "attributes": {"sla_days": 3}},
        {"id": IDS["b-1"], "kind": "invoice_batch", "name": "March batch",
         "attributes": {"amount": 1500.00, "currency": "PKR",
                        "status": "awaiting_approval", "due_at": "2026-12-02"}},
        {"id": IDS["wr-1"], "kind": "workflow_run", "name": "Invoice run 7",
         "attributes": {"status": "running"}},
    ]
    relationships = [
        {"id": "e-1", "source_id": IDS["p-1"], "target_id": IDS["pr-1"], "kind": "approves", "confidence": 1.0},
        {"id": "e-2", "source_id": IDS["p-1"], "target_id": IDS["pr-2"], "kind": "owns", "confidence": 1.0},
        {"id": "e-3", "source_id": IDS["p-1"], "target_id": IDS["pr-3"], "kind": "responsible_for", "confidence": 1.0},
        {"id": "e-4", "source_id": IDS["p-2"], "target_id": IDS["pr-1"], "kind": "backup_for", "confidence": 1.0},
        {"id": "e-5", "source_id": IDS["pr-1"], "target_id": IDS["sys-1"], "kind": "depends_on", "confidence": 1.0},
        {"id": "e-6", "source_id": IDS["sup-1"], "target_id": IDS["sys-1"], "kind": "supplies", "confidence": 1.0},
        {"id": "e-7", "source_id": IDS["b-1"], "target_id": IDS["pr-1"], "kind": "depends_on", "confidence": 1.0},
        {"id": "e-8", "source_id": IDS["wr-1"], "target_id": IDS["pr-1"], "kind": "depends_on", "confidence": 1.0},
        {"id": "e-9", "source_id": IDS["p-1"], "target_id": IDS["t-1"], "kind": "member_of", "confidence": 1.0},
        {"id": "e-10", "source_id": IDS["p-2"], "target_id": IDS["t-1"], "kind": "member_of", "confidence": 1.0},
    ]
    return {
        "entities": [e for e in entities if e["id"] not in {IDS[r] for r in remove}],
        "relationships": [r for r in relationships
                          if r["source_id"] not in {IDS[x] for x in remove}
                          and r["target_id"] not in {IDS[x] for x in remove}],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _forecast(snapshot=None):
    return forecast_unavailability(snapshot or _snapshot(), [IDS["p-1"]], now=NOW)


def _scan_result(forecast, snapshot):
    return {
        "scanned_at": NOW.isoformat(),
        "forecasts": [{
            "root_id": IDS["p-1"], "root_kind": "person", "horizon_days": 7,
            "fingerprint": forecast["fingerprint"], "forecast": forecast,
        }],
    }


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def seed():
    async def go(sm):
        async with sm() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            admin_p = platform.Principal(admin.id, admin.email, "user", "web", admin.auth_version)
            org = await platform.ensure_org_scope(db)
            await platform.ensure_role(db, "forecasts_viewer", {"forecasts.read"})
            await platform.ensure_role(db, "plain_member", set())
            tenant = GraphTenant(name="Acme")
            db.add(tenant)
            await db.flush()
            for email, role in ((ADMIN_EMAIL, "owner"), (USER_EMAIL, "viewer"),
                                (USER2_EMAIL, "viewer")):
                user_id = await db.scalar(select(User.id).where(User.email == email))
                db.add(GraphMember(tenant_id=tenant.id, user_id=user_id, role=role))
            for email, role in ((ADMIN_EMAIL, "forecasts_viewer"),
                                (USER_EMAIL, "forecasts_viewer"),
                                (USER2_EMAIL, "plain_member")):
                user_id = await db.scalar(select(User.id).where(User.email == email))
                await platform.grant_membership(db, admin_p, user_id=user_id,
                                               scope_id=org.id, role_key=role)
            # A second tenant with no members except the owner, for 404s.
            tenant_b = GraphTenant(name="Other")
            db.add(tenant_b)
            await db.flush()
            db.add(GraphMember(tenant_id=tenant_b.id, user_id=admin.id, role="owner"))
            kinds = {"p-1": "person", "p-2": "person", "t-1": "team", "sys-1": "system",
                     "sup-1": "supplier", "pr-1": "process", "pr-2": "process",
                     "pr-3": "process", "b-1": "invoice_batch", "wr-1": "workflow_run"}
            names = {"p-1": "Ayesha", "p-2": "Bilal", "t-1": "Finance", "sys-1": "ERP",
                     "sup-1": "Acme Supplies", "pr-1": "Pay suppliers",
                     "pr-2": "Onboard vendor", "pr-3": "File tax return",
                     "b-1": "March batch", "wr-1": "Invoice run 7"}
            for key, eid in IDS.items():
                db.add(GraphRecord(
                    id=uuid.UUID(eid), tenant_id=tenant.id, record_type="entity",
                    external_key=eid, kind=kinds[key], name=names[key],
                    visibility="restricted" if key in ("pr-2", "b-1") else "tenant",
                    readers=[], valid_from=NOW, observed_at=NOW))
            await db.commit()
            return {"tenant_id": tenant.id, "tenant_b_id": tenant_b.id}

    return run(go)


async def _user(db, email):
    return await db.scalar(select(User).where(User.email == email))


# --- Pure visibility filter -------------------------------------------------

def test_filter_hides_restricted_process_and_money():
    forecast = _forecast()
    assert forecast["counts"]["processes"] == 3
    assert forecast["money_at_risk"] == {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0}
    visible = {v for k, v in IDS.items() if k not in ("pr-2", "b-1")}
    filtered = filter_forecast_visibility(forecast, visible, snapshot=_snapshot())
    assert [p["id"] for p in filtered["processes"]] == [IDS["pr-3"], IDS["pr-1"]]
    assert [p["n"] for p in filtered["processes"]] == [1, 2]  # renumbered
    assert filtered["counts"] == {
        "processes": 2, "processes_due_within_horizon": 2,
        "affected": 3, "uncovered": 2,  # pr-1 is covered by Bilal, so only pr-3 + wr-1 stay uncovered
    }
    assert filtered["money_at_risk"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    assert filtered["money_at_risk_due"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    pr1 = next(p for p in filtered["processes"] if p["id"] == IDS["pr-1"])
    assert pr1["money_at_risk"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    assert "Onboard vendor" not in str(filtered)
    assert "March batch" not in str(filtered)


def test_filter_without_snapshot_still_never_leaks_money():
    forecast = _forecast()
    visible = {v for k, v in IDS.items() if k not in ("pr-2", "b-1")}
    filtered = filter_forecast_visibility(forecast, visible)  # no snapshot: path fallback
    pr1 = next(p for p in filtered["processes"] if p["id"] == IDS["pr-1"])
    assert pr1["money_at_risk"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    assert filtered["money_at_risk"]["totals"] == {}


def test_filter_hides_hidden_root_name_and_owner():
    forecast = _forecast()
    visible = {v for k, v in IDS.items() if k != "p-1"}
    filtered = filter_forecast_visibility(forecast, visible, snapshot=_snapshot())
    assert filtered["unavailable"] == [
        {"id": IDS["p-1"], "name": IDS["p-1"], "kind": "person"}]
    pr1 = next(p for p in filtered["processes"] if p["id"] == IDS["pr-1"])
    assert pr1["owner_ids"] == []
    assert pr1["owner_names"] == []
    assert "Ayesha" not in str(filtered)


def test_filter_drops_hidden_backup_names():
    forecast = _forecast()
    pr1 = next(p for p in forecast["processes"] if p["id"] == IDS["pr-1"])
    assert pr1["covered_by"] == [{"person_id": IDS["p-2"], "name": "Bilal"}]
    visible = {v for k, v in IDS.items() if k != "p-2"}
    filtered = filter_forecast_visibility(forecast, visible, snapshot=_snapshot())
    pr1 = next(p for p in filtered["processes"] if p["id"] == IDS["pr-1"])
    assert pr1["covered_by"] == []
    assert "Bilal" not in str(filtered)


def test_filter_none_returns_unchanged_copy():
    forecast = _forecast()
    filtered = filter_forecast_visibility(forecast, None)
    assert filtered == forecast
    assert filtered is not forecast
    filtered["counts"]["processes"] = 99
    assert forecast["counts"]["processes"] == 3


# --- Record service ---------------------------------------------------------

def test_record_scan_creates_and_is_idempotent(seed):
    async def go(sm):
        async with sm() as db:
            admin = await _user(db, ADMIN_EMAIL)
            snap = _snapshot()
            forecast = _forecast(snap)
            first = await record_scan(db, seed["tenant_id"], _scan_result(forecast, snap),
                                      snap, created_by=admin.id)
            second = await record_scan(db, seed["tenant_id"], _scan_result(forecast, snap),
                                       snap, created_by=admin.id)
            await db.commit()
            assert len(first) == len(second) == 1
            assert first[0].id == second[0].id
            assert first[0].fingerprint == forecast["fingerprint"]
            assert first[0].superseded is False
            assert first[0].root_id == IDS["p-1"]
            assert first[0].horizon_days == 7
            count = await db.scalar(select(func.count()).select_from(ForecastRecord))
            assert count == 1

    run(go)


def test_record_scan_supersedes_and_scores_drift(seed):
    async def go(sm):
        async with sm() as db:
            snap1 = _snapshot()
            forecast1 = _forecast(snap1)
            await record_scan(db, seed["tenant_id"], _scan_result(forecast1, snap1), snap1)
            snap2 = _snapshot(remove=("pr-2", "b-1", "wr-1"))
            forecast2 = _forecast(snap2)
            records = await record_scan(db, seed["tenant_id"], _scan_result(forecast2, snap2), snap2)
            await db.commit()
            assert len(records) == 1
            rows = await db.scalars(select(ForecastRecord))
            rows = list(rows)
            assert len(rows) == 2
            # Identify by fingerprint, not by created_at order: both
            # rows are written in one transaction, and created_at uses
            # now() (the transaction start), so the timestamps tie.
            old = next(r for r in rows if r.fingerprint == forecast1["fingerprint"])
            new = next(r for r in rows if r.fingerprint == forecast2["fingerprint"])
            assert old.superseded is True
            # Removed: 3 entities (pr-2, b-1, wr-1) x 10 = 30; removed
            # edges (e-2, e-7, e-8) x 3 = 9; money PKR 1500 -> 0 is a
            # 100% move x 20 = 20. Total 59 -> review band.
            assert old.drift_score == 59
            assert new.superseded is False
            assert new.drift_score is None
            current = await list_forecasts(db, seed["tenant_id"], await _user(db, ADMIN_EMAIL))
            assert len(current) == 1 and current[0]["id"] == str(new.id)
            assert current[0]["trust"] == "trustworthy"  # current record
            all_rows = await list_forecasts(db, seed["tenant_id"], await _user(db, ADMIN_EMAIL),
                                            include_superseded=True)
            assert len(all_rows) == 2
            old_row = next(item for item in all_rows if item["superseded"])
            assert old_row["drift_score"] == 59
            assert old_row["trust"] == "review"

    run(go)


def test_list_owner_sees_everything(seed):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            await record_scan(db, seed["tenant_id"], _scan_result(_forecast(snap), snap), snap)
            await db.commit()
            items = await list_forecasts(db, seed["tenant_id"], await _user(db, ADMIN_EMAIL))
            assert len(items) == 1
            item = items[0]
            assert item["trust"] == "trustworthy"  # current record: freshest scan
            assert item["drift_score"] is None
            forecast = item["forecast"]
            assert [p["id"] for p in forecast["processes"]] == [IDS["pr-3"], IDS["pr-2"], IDS["pr-1"]]
            assert forecast["money_at_risk"]["totals"] == {"PKR": 1500.0}

    run(go)


def test_list_viewer_filters_hidden(seed):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            await record_scan(db, seed["tenant_id"], _scan_result(_forecast(snap), snap), snap)
            await db.commit()
            items = await list_forecasts(db, seed["tenant_id"], await _user(db, USER_EMAIL))
            assert len(items) == 1
            forecast = items[0]["forecast"]
            assert [p["id"] for p in forecast["processes"]] == [IDS["pr-3"], IDS["pr-1"]]
            assert forecast["counts"]["processes"] == 2
            assert forecast["counts"]["affected"] == 3
            assert forecast["money_at_risk"]["totals"] == {}
            assert "Onboard vendor" not in str(forecast)
            assert "March batch" not in str(forecast)

    run(go)


def test_get_one_viewer_filtered_and_owner_full(seed):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            records = await record_scan(db, seed["tenant_id"], _scan_result(_forecast(snap), snap), snap)
            await db.commit()
            record_id = records[0].id
            viewer = await get_forecast(db, seed["tenant_id"], record_id, await _user(db, USER_EMAIL))
            assert viewer["forecast"]["counts"]["processes"] == 2
            owner = await get_forecast(db, seed["tenant_id"], record_id, await _user(db, ADMIN_EMAIL))
            assert owner["forecast"]["counts"]["processes"] == 3
            assert owner["forecast"]["money_at_risk"]["totals"] == {"PKR": 1500.0}

    run(go)


def test_member_without_forecasts_read_gets_403(seed):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            await record_scan(db, seed["tenant_id"], _scan_result(_forecast(snap), snap), snap)
            await db.commit()
            with pytest.raises(PermissionDeniedError):
                await list_forecasts(db, seed["tenant_id"], await _user(db, USER2_EMAIL))

    run(go)


def test_non_member_and_unknown_tenant_get_404(seed):
    async def go(sm):
        async with sm() as db:
            with pytest.raises(NotFoundError):
                await list_forecasts(db, seed["tenant_b_id"], await _user(db, USER_EMAIL))
            with pytest.raises(NotFoundError):
                await list_forecasts(db, uuid.uuid4(), await _user(db, USER_EMAIL))
            with pytest.raises(NotFoundError):
                await get_forecast(db, seed["tenant_id"], uuid.uuid4(), await _user(db, ADMIN_EMAIL))

    run(go)


def test_prune_keeps_30_and_deletes_aged(seed):
    async def go(sm):
        async with sm() as db:
            snap = _snapshot()
            forecast = _forecast(snap)
            base = {
                "tenant_id": seed["tenant_id"], "root_id": IDS["p-1"],
                "root_kind": "person", "horizon_days": 7,
                "forecast": forecast, "snapshot": snap, "as_of": NOW,
            }
            for i in range(35):  # 35 superseded records, newest first by created_at
                db.add(ForecastRecord(
                    **base, fingerprint=f"fp-{i:02d}", superseded=True,
                    created_at=NOW - timedelta(days=i)))
            for i in range(2):  # aged superseded records, older than 90 days
                db.add(ForecastRecord(
                    **base, fingerprint=f"old-{i}", superseded=True,
                    created_at=NOW - timedelta(days=100 + i)))
            db.add(ForecastRecord(**base, fingerprint="current", superseded=False,
                                  created_at=NOW))
            await db.commit()
            deleted = await prune_forecasts(db, seed["tenant_id"], now=NOW)
            await db.commit()
            # 2 aged + 5 beyond the newest 30 superseded = 7 deleted.
            assert deleted == 7
            remaining = await db.scalars(select(ForecastRecord))
            remaining = list(remaining)
            assert len(remaining) == 31  # 30 superseded + 1 current
            assert any(r.fingerprint == "current" and not r.superseded for r in remaining)
            assert all(r.fingerprint != "old-0" and r.fingerprint != "old-1" for r in remaining)

    run(go)
