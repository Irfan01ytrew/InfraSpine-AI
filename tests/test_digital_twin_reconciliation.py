"""Drill reconciliation review queue (Step K v2 slice 5).

DB-backed tests on a throwaway *_test database only. Proves the
reconciliation service (evaluate / idempotent record / list +
filters / counts / acknowledge / prune) and the API (tenant scope
via ``scope_for``, owner-only money, 404/401 behaviour).
Acknowledging a finding changes only the queue row — the twin and
the drill record are never touched.
"""

import uuid
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import test_engine_options
from app.db.twin_reconciliation_models import TwinReconciliationFinding
from app.digital_twin import reconciliation_records
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER_EMAIL, login, run_db
from tests.test_company_graph import checked, member, tenant
from tests.test_digital_twin_reconciliation_pure import (
    FP,
    RUN,
    _drill,
    _g_comparison,
    _simulation,
)
from tests.test_digital_twin_versions import run

FP2 = "c" * 64


def _finding_count():
    async def count(sm):
        async with sm() as db:
            return (await db.scalar(select(func.count()).select_from(TwinReconciliationFinding))) or 0

    return run(count)


async def _evaluate(sm, tid, **kwargs):
    async with sm() as db:
        result = await reconciliation_records.evaluate_reconciliation(
            db, tid, **kwargs)
        await db.commit()
        return result


def _mismatch_kwargs(*, fingerprint=FP, run_id=RUN):
    """A money-mismatch simulation + one critical G timing finding."""
    return {
        "simulation": _simulation(money={"PKR": 120000.0}, fingerprint=fingerprint),
        "drill_observation": _drill(money={"PKR": 100000.0}),
        "g_comparison": _g_comparison(
            differences=[{"code": "rto_missed_in_drill", "resource_id": "e-erp",
                          "detail": "Observed recovery 90 minutes exceeded the RTO of 60 minutes."}],
            matches=False),
        "drill_run_id": run_id,
        "scenario_kind": "server_outage",
    }


# ---------------------------------------------------------------- service


def test_service_evaluate_stores_findings_and_counts():
    tid = uuid.uuid4()
    result = run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    assert result["matches"] is False
    assert result["twin_matches"] is False
    assert result["g_matches"] is False
    assert len(result["recorded"]["created"]) == 2
    assert result["recorded"]["existing"] == []
    assert result["counts"]["total"] == 2
    assert result["counts"]["critical"] == 1
    assert result["counts"]["warning"] == 1
    assert _finding_count() == 2

    async def counts(sm):
        async with sm() as db:
            return await reconciliation_records.reconciliation_counts(db, tid)

    assert run(counts) == {"total": 2, "unacknowledged": 2, "acknowledged": 0,
                           "info": 0, "warning": 1, "critical": 1,
                           "unacknowledged_critical": 1}


def test_service_evaluate_is_idempotent():
    tid = uuid.uuid4()
    first = run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    second = run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    assert len(first["recorded"]["created"]) == 2
    assert second["recorded"]["created"] == []
    assert len(second["recorded"]["existing"]) == 2
    assert _finding_count() == 2


def test_service_evaluate_match_stores_nothing():
    tid = uuid.uuid4()
    result = run(lambda sm: _evaluate(
        sm, tid, simulation=_simulation(), drill_observation=_drill(),
        drill_run_id=RUN))
    assert result["matches"] is True
    assert result["recorded"]["findings"] == []
    assert _finding_count() == 0


def test_service_new_simulation_of_same_run_is_new_findings():
    tid = uuid.uuid4()
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    second = run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs(fingerprint=FP2)))
    assert len(second["recorded"]["created"]) == 2
    assert _finding_count() == 4


def test_service_list_filters_and_acknowledge():
    tid = uuid.uuid4()
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    by = uuid.uuid4()

    async def listing(sm, **kwargs):
        async with sm() as db:
            return await reconciliation_records.list_findings(db, tid, **kwargs)

    findings = run(lambda sm: listing(sm))
    assert len(findings) == 2
    assert run(lambda sm: listing(sm, severity="critical"))[0]["code"] == "rto_missed_in_drill"
    assert run(lambda sm: listing(sm, severity="warning"))[0]["code"] == "money_mismatch"
    assert run(lambda sm: listing(sm, acknowledged=True)) == []
    assert len(run(lambda sm: listing(sm, acknowledged=False))) == 2

    async def acknowledge(sm, finding_id, who):
        async with sm() as db:
            summary = await reconciliation_records.acknowledge_finding(
                db, tid, UUID(finding_id), by=who)
            await db.commit()
            return summary

    critical = run(lambda sm: listing(sm, severity="critical"))[0]
    done = run(lambda sm: acknowledge(sm, critical["id"], by))
    assert done["acknowledged"] is True
    assert done["acknowledged_by"] == str(by)
    assert done["acknowledged_at"] is not None
    assert len(run(lambda sm: listing(sm, acknowledged=True))) == 1
    assert len(run(lambda sm: listing(sm, acknowledged=False))) == 1
    assert run(lambda sm: acknowledge(sm, str(uuid.uuid4()), by)) is None

    async def acknowledge_bad(sm):
        async with sm() as db:
            await reconciliation_records.acknowledge_finding(db, tid, "not-a-uuid", by=by)

    with pytest.raises(ValueError, match="finding_id must be a UUID"):
        run(acknowledge_bad)


def test_service_prune_and_validation():
    tid = uuid.uuid4()
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs(fingerprint=FP2)))

    async def prune(sm, **kwargs):
        async with sm() as db:
            pruned = await reconciliation_records.prune_findings(db, tid, **kwargs)
            await db.commit()
            return pruned

    assert _finding_count() == 4
    assert run(lambda sm: prune(sm, keep=2)) == 2
    assert _finding_count() == 2
    with pytest.raises(ValueError, match="keep must be at least 1"):
        run(lambda sm: prune(sm, keep=0))
    with pytest.raises(ValueError, match="must not be negative"):
        run(lambda sm: prune(sm, max_age_days=-1))
    with pytest.raises(ValueError, match="tenant_id must be a UUID"):
        run(lambda sm: _evaluate(sm, "not-a-uuid", **_mismatch_kwargs()))
    with pytest.raises(ValueError, match="must be a reconcile"):
        run(lambda sm: _record_bad(sm, tid))


async def _record_bad(sm, tid):
    async with sm() as db:
        await reconciliation_records.record_reconciliation(db, tid, report={"nope": True})


# --------------------------------------------------------------------- API


def _seed_findings(admin_client):
    """Tenant + one evaluated mismatch; returns (tenant_id, findings)."""
    t = tenant(admin_client, "Twin Reconciliation Atlas")
    tid = UUID(t)
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))
    return t, tid


def test_api_findings_owner_sees_money(admin_client):
    t, _tid = _seed_findings(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/reconciliation/findings"))
    assert body["tenant_id"] == t
    assert body["count"] == 2
    money = [item for item in body["findings"] if item["code"] == "money_mismatch"][0]
    assert money["money_difference"] == {"PKR": 20000.0}
    assert money["money_redacted"] is False
    assert money["severity"] == "warning"
    assert money["source"] == "twin"
    assert money["acknowledged"] is False


def test_api_findings_viewer_money_is_redacted(admin_client):
    t, _tid = _seed_findings(admin_client)
    member(admin_client, t, USER_EMAIL, role="viewer")
    login(admin_client, USER_EMAIL)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/reconciliation/findings"))
    assert body["count"] == 2
    money = [item for item in body["findings"] if item["code"] == "money_mismatch"][0]
    assert money["money_difference"] is None
    assert money["money_redacted"] is True
    assert money["detail"] == "money at risk differs for PKR"


def test_api_counts_and_filters(admin_client):
    t, _tid = _seed_findings(admin_client)
    counts = checked(admin_client.get(f"/api/digital-twin/{t}/reconciliation/counts"))
    assert counts["counts"]["total"] == 2
    assert counts["counts"]["unacknowledged_critical"] == 1
    critical = checked(admin_client.get(
        f"/api/digital-twin/{t}/reconciliation/findings?severity=critical"))
    assert critical["count"] == 1
    assert critical["findings"][0]["code"] == "rto_missed_in_drill"
    unseen = checked(admin_client.get(
        f"/api/digital-twin/{t}/reconciliation/findings?acknowledged=false"))
    assert unseen["count"] == 2
    seen = checked(admin_client.get(
        f"/api/digital-twin/{t}/reconciliation/findings?acknowledged=true"))
    assert seen["count"] == 0
    bad = admin_client.get(f"/api/digital-twin/{t}/reconciliation/findings?severity=nope")
    assert bad.status_code == 422, bad.text


def test_api_acknowledge(admin_client):
    t, _tid = _seed_findings(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/reconciliation/findings"))
    finding_id = body["findings"][0]["id"]
    done = checked(admin_client.post(
        f"/api/digital-twin/{t}/reconciliation/findings/{finding_id}/acknowledge"))
    assert done["finding"]["acknowledged"] is True
    assert done["finding"]["acknowledged_by"] is not None
    assert done["finding"]["id"] == finding_id
    unseen = checked(admin_client.get(
        f"/api/digital-twin/{t}/reconciliation/findings?acknowledged=false"))
    assert unseen["count"] == 1
    missing = admin_client.post(
        f"/api/digital-twin/{t}/reconciliation/findings/{uuid.uuid4()}/acknowledge")
    assert missing.status_code == 404, missing.text


def test_api_findings_unknown_tenant_is_404(admin_client):
    response = admin_client.get(
        f"/api/digital-twin/{uuid.uuid4()}/reconciliation/findings")
    assert response.status_code == 404, response.text
    response = admin_client.post(
        f"/api/digital-twin/{uuid.uuid4()}/reconciliation/findings/{uuid.uuid4()}/acknowledge")
    assert response.status_code == 404, response.text


def test_api_findings_unauthenticated_is_401(client):
    response = client.get(f"/api/digital-twin/{uuid.uuid4()}/reconciliation/findings")
    assert response.status_code == 401, response.text
