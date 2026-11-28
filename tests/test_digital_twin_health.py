"""Twin health score + watch service + API (Step K v2 slice 3).

DB-backed tests on a throwaway *_test database only. Proves the
health service (latest / history / watch evaluation + alert
recording / dedupe / cooldown / quiet hours / pruning) and the
read-only API (tenant scope via ``scope_for``, 404/401 behaviour,
no money amounts anywhere in a health response).
"""

import asyncio
import uuid
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import test_engine_options
from app.db.twin_health_models import TwinHealthAlert
from app.digital_twin import health_records
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
    run_db,
)
from tests.test_company_graph import checked, member, tenant
from tests.test_digital_twin_versions import _record, _seed_versions, _snapshot, run

PKT = ZoneInfo("Asia/Karachi")


def _alert_count():
    async def count(sm):
        async with sm() as db:
            return (await db.scalar(select(func.count()).select_from(TwinHealthAlert))) or 0

    return run(count)


async def _latest(sm, tid):
    async with sm() as db:
        return await health_records.latest_health(db, tid)


async def _history(sm, tid, **kwargs):
    async with sm() as db:
        return await health_records.health_history(db, tid, **kwargs)


async def _evaluate(sm, tid, now):
    async with sm() as db:
        result = await health_records.evaluate_and_record_health_watch(db, tid, now=now)
        await db.commit()
        return result


def _big_entities():
    return [
        {"id": f"e-n{i}", "kind": "employee", "name": f"N{i}", "attributes": {}}
        for i in range(6)
    ] + [{"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
          "attributes": {"amount": 100000, "currency": "PKR"}}]


def _big_snapshot(entities):
    return {"entities": entities, "relationships": [],
            "as_of": "2026-11-28T00:00:00Z", "stale_sources": []}


def _seed_critical_versions(tid):
    """Base + drift-63 version (health 37, trust stale)."""
    run(lambda sm: _record(sm, tid, _big_snapshot(_big_entities())))
    kept = [dict(entity) for entity in _big_entities()[4:6]]
    kept[0] = {**kept[0], "name": "N4 renamed"}
    return run(lambda sm: _record(sm, tid, _big_snapshot(kept)))


def _seed_degraded_versions(tid):
    """Base + drift-26 version (health 74, trust review)."""
    base = _snapshot(extra_entities=[
        {"id": "e-x", "kind": "employee", "name": "X", "attributes": {}},
        {"id": "e-y", "kind": "employee", "name": "Y", "attributes": {}},
    ])
    run(lambda sm: _record(sm, tid, base))
    second = _snapshot(entities=[
        {"id": "e-ayesha", "kind": "employee", "name": "Ayesha v2", "attributes": {"department": "Finance"}},
        {"id": "e-erp", "kind": "system", "name": "ERP v2", "attributes": {}},
        {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
         "attributes": {"amount": 100000, "currency": "PKR"}},
    ])
    return run(lambda sm: _record(sm, tid, second))


# ---------------------------------------------------------------- service


def test_latest_health_first_version_is_100():
    tid = uuid.uuid4()
    run(lambda sm: _record(sm, tid, _snapshot()))
    assessment = run(lambda sm: _latest(sm, tid))
    assert assessment["health_score"] == 100
    assert assessment["band"] == "healthy"
    assert assessment["is_first_version"] is True
    assert assessment["drift_score"] is None
    assert assessment["components"] == {"drift": 0, "money": 0, "freshness": 0}


def test_latest_health_after_drift_11():
    tid = uuid.uuid4()
    run(lambda sm: _record(sm, tid, _snapshot()))
    run(lambda sm: _record(
        sm, tid,
        _snapshot(erp_name="ERP v2", batch_amount=120000,
                  extra_entities=[{"id": "e-backup", "kind": "employee", "name": "Backup", "attributes": {}}]),
    ))
    assessment = run(lambda sm: _latest(sm, tid))
    assert assessment["health_score"] == 89
    assert assessment["band"] == "healthy"
    assert assessment["drift_score"] == 11
    assert assessment["components"] == {"drift": 8, "money": 3, "freshness": 0}


def test_latest_health_stale_source_penalty():
    tid = uuid.uuid4()
    run(lambda sm: _record(
        sm, tid, _snapshot(),
        source_freshness=[{"name": "erp", "status": "stale"}]))
    assessment = run(lambda sm: _latest(sm, tid))
    assert assessment["health_score"] == 90
    assert assessment["stale_sources"] == ["erp"]
    assert assessment["components"]["freshness"] == 10


def test_latest_health_no_versions_is_none():
    assert run(lambda sm: _latest(sm, uuid.uuid4())) is None


def test_health_history_newest_first():
    tid = uuid.uuid4()
    summaries = [
        run(lambda sm, n=n: _record(sm, tid, _snapshot(erp_name=f"ERP v{n}")))
        for n in range(3)
    ]
    history = run(lambda sm: _history(sm, tid))
    assert len(history) == 3
    assert [entry["fingerprint"] for entry in history] == [
        s["fingerprint"] for s in reversed(summaries)]
    assert history[-1]["is_first_version"] is True
    assert history[-1]["health_score"] == 100
    assert all(0 <= entry["health_score"] <= 100 for entry in history)
    assert "money" not in history[0]  # no amounts anywhere in history


def test_watch_service_fires_critical_and_dedupes():
    tid = uuid.uuid4()
    _seed_critical_versions(tid)
    now = datetime(2026, 11, 28, 10, 0, tzinfo=PKT)
    first = run(lambda sm: _evaluate(sm, tid, now))
    assert first["health"]["health_score"] == 37
    assert sorted(alert.rule_key for alert in first["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    assert len(first["recorded"]) == 3
    assert _alert_count() == 3

    second = run(lambda sm: _evaluate(sm, tid, now))
    assert second["alerts"] == []  # inside every cooldown: suppressed, not refired
    assert sorted(entry["rule_key"] for entry in second["suppressed"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    assert _alert_count() == 3  # no duplicate rows for the same version


def test_watch_service_quiet_hours_defers_warning():
    tid = uuid.uuid4()
    _seed_degraded_versions(tid)
    night = datetime(2026, 11, 28, 23, 0, tzinfo=PKT)
    result = run(lambda sm: _evaluate(sm, tid, night))
    assert result["health"]["health_score"] == 74
    assert result["alerts"] == []
    assert [entry["rule_key"] for entry in result["deferred"]] == ["health_degraded"]
    assert result["recorded"] == [] and _alert_count() == 0

    day = datetime(2026, 11, 29, 10, 0, tzinfo=PKT)
    result = run(lambda sm: _evaluate(sm, tid, day))
    assert [alert.rule_key for alert in result["alerts"]] == ["health_degraded"]
    assert len(result["recorded"]) == 1 and _alert_count() == 1


def test_watch_service_cooldown_expiry_refires_without_duplicating():
    tid = uuid.uuid4()
    _seed_critical_versions(tid)
    first = run(lambda sm: _evaluate(sm, tid, datetime(2026, 11, 28, 10, 0, tzinfo=PKT)))
    assert len(first["recorded"]) == 3
    before = {row["fingerprint"]: row["evaluated_at"] for row in first["recorded"]}
    run_db(lambda conn: conn.execute(text(
        "UPDATE twin_health_alerts SET evaluated_at = now() - interval '5 hours',"
        " created_at = now() - interval '5 hours'")))

    later = run(lambda sm: _evaluate(sm, tid, datetime(2026, 11, 28, 13, 0, tzinfo=PKT)))
    assert sorted(alert.rule_key for alert in later["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    assert _alert_count() == 3  # refire refreshes the rows, never duplicates
    after = {row["fingerprint"]: row["evaluated_at"] for row in later["recorded"]}
    assert all(after[fp] > before[fp] for fp in before)


def test_watch_service_no_versions_fires_nothing():
    result = run(lambda sm: _evaluate(sm, uuid.uuid4(), datetime(2026, 11, 28, 10, 0, tzinfo=PKT)))
    assert result["health"] is None
    assert result["alerts"] == [] and result["recorded"] == []
    assert result["reason"] == "no twin versions recorded"


def test_prune_alerts_keeps_newest_two():
    tid = uuid.uuid4()
    _seed_degraded_versions(tid)  # 1 warning alert
    run(lambda sm: _evaluate(sm, tid, datetime(2026, 11, 28, 10, 0, tzinfo=PKT)))
    _seed_critical_versions(tid)  # same tenant, new versions -> 3 more alerts
    run(lambda sm: _evaluate(sm, tid, datetime(2026, 11, 28, 10, 0, tzinfo=PKT)))
    assert _alert_count() == 4

    async def prune(sm):
        async with sm() as db:
            deleted = await health_records.prune_health_alerts(db, tid, keep=2)
            await db.commit()
            return deleted

    assert run(prune) == 2
    assert _alert_count() == 2


# ------------------------------------------------------------------- API


def test_api_health_latest_owner(admin_client):
    t, summaries = _seed_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/health"))
    assert body["tenant_id"] == t
    assert body["health"]["health_score"] == 89
    assert body["health"]["band"] == "healthy"
    assert body["health"]["fingerprint"] == summaries[1]["fingerprint"]
    assert body["health"]["components"] == {"drift": 8, "money": 3, "freshness": 0}


def test_api_health_first_version(admin_client):
    t, summaries = _seed_versions(admin_client, count=1)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/health"))
    assert body["health"]["health_score"] == 100
    assert body["health"]["is_first_version"] is True


def test_api_health_no_versions_is_404(admin_client):
    t = tenant(admin_client, "Twin Health Atlas Empty")
    response = admin_client.get(f"/api/digital-twin/{t}/health")
    assert response.status_code == 404, response.text


def test_api_health_history(admin_client):
    t, summaries = _seed_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/health/history"))
    assert body["count"] == 2
    assert [entry["health_score"] for entry in body["history"]] == [89, 100]
    assert body["history"][1]["is_first_version"] is True


def test_api_health_alerts_flow(admin_client):
    t = tenant(admin_client, "Twin Health Atlas Alerts")
    tid = UUID(t)
    _seed_critical_versions(tid)
    run(lambda sm: _evaluate(sm, tid, datetime(2026, 11, 28, 10, 0, tzinfo=PKT)))
    body = checked(admin_client.get(f"/api/digital-twin/{t}/health/alerts"))
    assert body["count"] == 3
    assert sorted(alert["rule_key"] for alert in body["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    for alert in body["alerts"]:
        assert "PKR" not in alert["detail"]  # no money amounts in alerts
        assert alert["health_score"] == 37


def test_api_health_viewer_can_read_health(admin_client):
    t, _ = _seed_versions(admin_client)
    member(admin_client, t, USER_EMAIL, role="viewer")
    login(admin_client, USER_EMAIL)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/health"))
    assert body["health"]["health_score"] == 89
    alerts = checked(admin_client.get(f"/api/digital-twin/{t}/health/alerts"))
    assert alerts["count"] == 0


def test_api_health_unknown_tenant_is_404(admin_client):
    response = admin_client.get(f"/api/digital-twin/{uuid.uuid4()}/health")
    assert response.status_code == 404, response.text


def test_api_health_non_member_is_404(admin_client):
    t, _ = _seed_versions(admin_client)
    login(admin_client, USER2_EMAIL)  # seeded user, not a tenant member
    response = admin_client.get(f"/api/digital-twin/{t}/health")
    assert response.status_code == 404, response.text


def test_api_health_unauthenticated_is_401(client):
    response = client.get(f"/api/digital-twin/{uuid.uuid4()}/health")
    assert response.status_code == 401, response.text
