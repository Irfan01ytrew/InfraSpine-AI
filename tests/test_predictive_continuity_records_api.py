"""Tests for Step L slice 3: the read-only forecast-record API.

Runs against real PostgreSQL (the conftest migrates TEST_DATABASE_URL
to head, including 0106_forecast_records) and the real FastAPI app via
the conftest ``client`` fixture. The app is pointed at the test
database by the conftest itself.

Setup per test: a graph tenant with an owner (ADMIN_EMAIL), a viewer
(USER_EMAIL) holding ``forecasts.read``, and a second viewer
(USER2_EMAIL) who is a graph member but holds no ``forecasts.read``.
Two snapshot entities are restricted (``pr-2``, ``b-1``). One forecast
record is stored via ``record_scan`` before each HTTP test.

The zero-side-effect test dumps every ``forecast_records`` row before
and after a burst of GETs and requires the dumps to be identical:
reads must never write.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.company_graph.models import GraphMember, GraphRecord, GraphTenant
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity.forecast import forecast_unavailability
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.records import record_scan
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
)
from tests.test_predictive_continuity_records import IDS, _forecast, _snapshot

pytestmark = pytest.mark.usefixtures("clean_tables")

NOW = datetime(2026, 11, 28, tzinfo=UTC)


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
            snap = _snapshot()
            forecast = forecast_unavailability(snap, [IDS["p-1"]], now=NOW)
            records = await record_scan(db, tenant.id, {
                "scanned_at": NOW.isoformat(),
                "forecasts": [{
                    "root_id": IDS["p-1"], "root_kind": "person", "horizon_days": 7,
                    "fingerprint": forecast["fingerprint"], "forecast": forecast,
                }],
            }, snap)
            await db.commit()
            return {"tenant_id": str(tenant.id), "tenant_b_id": str(tenant_b.id),
                    "record_id": str(records[0].id)}

    return run(go)


def _dump_records():
    async def go(sm):
        async with sm() as db:
            rows = await db.scalars(select(ForecastRecord).order_by(ForecastRecord.id))
            return json.dumps([
                {"id": str(r.id), "tenant_id": str(r.tenant_id), "root_id": r.root_id,
                 "fingerprint": r.fingerprint, "superseded": r.superseded,
                 "drift_score": r.drift_score,
                 "forecast": r.forecast, "snapshot": r.snapshot,
                 "as_of": r.as_of.isoformat() if r.as_of else None,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in rows
            ], sort_keys=True)

    return run(go)


def test_list_viewer_gets_filtered_forecast(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == seed["tenant_id"]
    assert body["count"] == 1
    item = body["forecasts"][0]
    assert item["id"] == seed["record_id"]
    assert item["root_id"] == IDS["p-1"]
    assert item["horizon_days"] == 7
    assert item["superseded"] is False
    assert item["trust"] == "trustworthy"
    forecast = item["forecast"]
    assert [p["id"] for p in forecast["processes"]] == [IDS["pr-3"], IDS["pr-1"]]
    assert forecast["counts"]["processes"] == 2
    assert forecast["money_at_risk"]["totals"] == {}
    assert "Onboard vendor" not in response.text
    assert "March batch" not in response.text


def test_list_owner_gets_full_forecast(client, seed):
    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts")
    assert response.status_code == 200, response.text
    forecast = response.json()["forecasts"][0]["forecast"]
    assert [p["id"] for p in forecast["processes"]] == [IDS["pr-3"], IDS["pr-2"], IDS["pr-1"]]
    assert forecast["money_at_risk"]["totals"] == {"PKR": 1500.0}


def test_get_one_viewer_filtered(client, seed):
    login(client, USER_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts/{seed['record_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["forecast"]["counts"]["processes"] == 2


def test_member_without_forecasts_read_gets_403(client, seed):
    login(client, USER2_EMAIL)
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts")
    assert response.status_code == 403, response.text
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts/{seed['record_id']}")
    assert response.status_code == 403, response.text


def test_non_member_and_unknown_ids_get_404(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/predictive-continuity/{seed['tenant_b_id']}/forecasts")
    assert response.status_code == 404, response.text
    response = client.get(f"/api/predictive-continuity/{uuid.uuid4()}/forecasts")
    assert response.status_code == 404, response.text
    login(client, ADMIN_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts/{uuid.uuid4()}")
    assert response.status_code == 404, response.text


def test_unauthenticated_gets_401(client, seed):
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts")
    assert response.status_code == 401, response.text


def test_include_superseded_flag(client, seed):
    # Store a second scan with a changed snapshot so the first record
    # is superseded, then check the list flag.
    async def go(sm):
        async with sm() as db:
            snap2 = _snapshot(remove=("pr-2", "b-1", "wr-1"))
            forecast2 = _forecast(snap2)
            await record_scan(db, uuid.UUID(seed["tenant_id"]), {
                "scanned_at": NOW.isoformat(),
                "forecasts": [{
                    "root_id": IDS["p-1"], "root_kind": "person", "horizon_days": 7,
                    "fingerprint": forecast2["fingerprint"], "forecast": forecast2,
                }],
            }, snap2)
            await db.commit()

    run(go)
    login(client, ADMIN_EMAIL)
    base = f"/api/predictive-continuity/{seed['tenant_id']}/forecasts"
    assert len(client.get(base).json()["forecasts"]) == 1
    superseded = client.get(base + "?include_superseded=true").json()["forecasts"]
    assert len(superseded) == 2
    old = next(item for item in superseded if item["superseded"])
    assert old["drift_score"] == 59
    assert old["trust"] == "review"


def test_reads_write_nothing(client, seed):
    """Zero-side-effect proof: GETs must not change forecast_records."""
    before = _dump_records()
    login(client, USER_EMAIL)
    assert client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts").status_code == 200
    assert client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts/{seed['record_id']}").status_code == 200
    assert client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts?include_superseded=true").status_code == 200
    login(client, ADMIN_EMAIL)
    assert client.get(f"/api/predictive-continuity/{seed['tenant_id']}/forecasts").status_code == 200
    assert client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/forecasts/{seed['record_id']}").status_code == 200
    after = _dump_records()
    assert after == before
