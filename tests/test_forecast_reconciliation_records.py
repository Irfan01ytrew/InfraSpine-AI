"""Forecast-vs-drill reconciliation (Step L slice 7).

DB-backed tests on a throwaway *_test database only. Proves the
review queue end to end: evaluate a stored forecast against a
Step G drill comparison (match / over-prediction / miss), the
idempotent store, the miss penalty raising the forecast record's
drift score (never lowering it), list/counts/acknowledge/prune,
and the API (tenant scope via ``forecasts.read``, owner-only
money, 404/401/403 behaviour).

Findings come from a real slice-1 forecast (p-1 unavailable,
horizon 7): processes pr-1 (due 2026-12-03, PKR 1500 at risk),
pr-2 (due 2026-12-20), pr-3 (SLA 3 days), plus sys-1, b-1 and
wr-1 as affected entities.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.company_graph.models import GraphMember, GraphRecord, GraphTenant
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity import reconciliation_records
from app.predictive_continuity.models import ForecastReconciliation, ForecastRecord
from app.predictive_continuity.records import record_scan
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
)
from tests.test_company_graph import tenant
from tests.test_predictive_continuity_records import IDS, _forecast, _snapshot

pytestmark = pytest.mark.usefixtures("clean_tables")

NOW = datetime(2026, 11, 28, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent
RUN1 = str(uuid.uuid4())
RUN2 = str(uuid.uuid4())
RUN3 = str(uuid.uuid4())
EXTRA = str(uuid.uuid4())


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
    """Tenant + members + platform roles + one stored p-1 forecast."""
    forecast = _forecast()
    snapshot = _snapshot()

    async def go(sm):
        async with sm() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            admin_p = platform.Principal(admin.id, admin.email, "user", "web", admin.auth_version)
            org = await platform.ensure_org_scope(db)
            await platform.ensure_role(db, "forecasts_viewer", {"forecasts.read"})
            await platform.ensure_role(db, "plain_member", set())
            tenant_row = GraphTenant(name="Acme")
            db.add(tenant_row)
            await db.flush()
            for email, role in ((ADMIN_EMAIL, "owner"), (USER_EMAIL, "viewer"),
                                (USER2_EMAIL, "viewer")):
                user_id = await db.scalar(select(User.id).where(User.email == email))
                db.add(GraphMember(tenant_id=tenant_row.id, user_id=user_id, role=role))
            for email, role in ((ADMIN_EMAIL, "forecasts_viewer"),
                                (USER_EMAIL, "forecasts_viewer"),
                                (USER2_EMAIL, "plain_member")):
                user_id = await db.scalar(select(User.id).where(User.email == email))
                await platform.grant_membership(db, admin_p, user_id=user_id,
                                                scope_id=org.id, role_key=role)
            kinds = {"p-1": "person", "p-2": "person", "t-1": "team", "sys-1": "system",
                     "sup-1": "supplier", "pr-1": "process", "pr-2": "process",
                     "pr-3": "process", "b-1": "invoice_batch", "wr-1": "workflow_run"}
            names = {"p-1": "Ayesha", "p-2": "Bilal", "t-1": "Finance", "sys-1": "ERP",
                     "sup-1": "Acme Supplies", "pr-1": "Pay suppliers",
                     "pr-2": "Onboard vendor", "pr-3": "File tax return",
                     "b-1": "March batch", "wr-1": "Invoice run 7"}
            for key, eid in IDS.items():
                db.add(GraphRecord(
                    id=uuid.UUID(eid), tenant_id=tenant_row.id, record_type="entity",
                    external_key=eid, kind=kinds[key], name=names[key],
                    visibility="tenant", readers=[], valid_from=NOW, observed_at=NOW))
            records = await record_scan(db, tenant_row.id, {
                "scanned_at": NOW.isoformat(),
                "forecasts": [{
                    "root_id": IDS["p-1"], "root_kind": "person", "horizon_days": 7,
                    "fingerprint": forecast["fingerprint"], "forecast": forecast,
                }],
            }, snapshot)
            await db.commit()
            return {"tenant_id": tenant_row.id, "record": records[0],
                    "forecast": forecast}

    result = run(go)
    return {"tenant_id": str(result["tenant_id"]), "tid": result["tenant_id"],
            "record_id": str(result["record"].id),
            "forecast": result["forecast"]}


def _affected_ids(forecast):
    return [entry["entity"]["id"] for entry in forecast["affected"]]


def _comparison(*, run_id, recovered, differences=(), affected=None):
    recovered = list(recovered)
    affected = list(recovered) if affected is None else list(affected)
    return {
        "run_id": run_id,
        "scenario_id": str(uuid.uuid4()),
        "drill": {
            "affected_ids": affected,
            "recovered_ids": recovered,
            "recovery_minutes": {}, "rto_minutes": {}, "mtpd_minutes": {},
            "data_loss_minutes": {}, "rpo_minutes": {},
        },
        "simulation": {"affected_ids": affected, "forecast": {}},
        "differences": list(differences),
        "matches": not differences,
        "affected": len(affected), "recovered": len(recovered),
        "simulated": len(affected),
    }


def _evaluate(seed, *, run_id, recovered, differences=()):
    async def go(sm):
        async with sm() as db:
            record = await db.get(ForecastRecord, UUID(seed["record_id"]))
            result = await reconciliation_records.evaluate_reconciliation(
                db, seed["tid"], forecast_record=record,
                g_comparison=_comparison(
                    run_id=run_id, recovered=recovered, differences=differences),
                drill_kind="resignation")
            await db.commit()
            return result

    return run(go)


def _record_drift(seed):
    async def go(sm):
        async with sm() as db:
            record = await db.get(ForecastRecord, UUID(seed["record_id"]))
            return record.drift_score

    return run(go)


def _reconciliation_count():
    async def count(sm):
        async with sm() as db:
            return (await db.scalar(
                select(func.count()).select_from(ForecastReconciliation))) or 0

    return run(count)


# -------------------------------------------------------------------------------- service

def test_service_match_stores_nothing_and_leaves_drift_alone(seed):
    result = _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]))
    assert result["matches"] is True
    assert result["recorded"]["created"] is True
    assert result["recorded"]["reconciliation"]["findings"] == []
    assert result["recorded"]["reconciliation"]["worst_severity"] is None
    assert result["adjusted_drift_score"] is None
    assert _record_drift(seed) is None
    assert _reconciliation_count() == 1


def test_service_over_prediction_and_g_difference_raise_drift(seed):
    affected = _affected_ids(seed["forecast"])
    recovered = [eid for eid in affected if eid != IDS["pr-1"]]
    differences = [{"code": "rto_missed_in_drill", "resource_id": IDS["sys-1"],
                    "detail": "Observed recovery 90 minutes exceeded the RTO of 60 minutes."}]
    result = _evaluate(seed, run_id=RUN1, recovered=recovered, differences=differences)
    assert result["matches"] is False
    assert result["counts"]["total"] == 2
    assert result["counts"]["critical"] == 2
    findings = {item["code"]: item for item in result["recorded"]["reconciliation"]["findings"]}
    assert findings["forecasted_not_drilled"]["resource_id"] == IDS["pr-1"]
    assert findings["forecasted_not_drilled"]["severity"] == "critical"
    assert findings["forecasted_not_drilled"]["money"] == {"PKR": 1500.0}
    assert "1500" not in findings["forecasted_not_drilled"]["detail"]
    assert findings["rto_missed_in_drill"]["severity"] == "critical"
    # Miss penalty: 10 (critical over-prediction) + 10 (critical G difference).
    assert result["adjusted_drift_score"] == 20
    assert _record_drift(seed) == 20


def test_service_evaluate_is_idempotent(seed):
    first = _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]))
    second = _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]))
    assert first["recorded"]["created"] is True
    assert second["recorded"]["created"] is False
    assert (second["recorded"]["reconciliation"]["id"]
            == first["recorded"]["reconciliation"]["id"])
    assert _reconciliation_count() == 1


def test_service_miss_costs_more_than_over_prediction(seed):
    result = _evaluate(seed, run_id=RUN1,
                       recovered=_affected_ids(seed["forecast"]) + [EXTRA])
    assert len(result["recorded"]["reconciliation"]["findings"]) == 1
    finding = result["recorded"]["reconciliation"]["findings"][0]
    assert finding["code"] == "drilled_not_forecasted"
    assert finding["resource_id"] == EXTRA
    assert result["adjusted_drift_score"] == 15
    assert _record_drift(seed) == 15


def test_service_second_reconciliation_only_raises_drift(seed):
    _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]) + [EXTRA])
    assert _record_drift(seed) == 15
    affected = _affected_ids(seed["forecast"])
    _evaluate(seed, run_id=RUN2, recovered=[eid for eid in affected if eid != IDS["pr-1"]])
    # 15 (miss) + 10 (critical over-prediction) = 25, never lowered.
    assert _record_drift(seed) == 25


def test_service_wrong_tenant_record_is_value_error(seed):
    async def go(sm):
        async with sm() as db:
            record = await db.get(ForecastRecord, UUID(seed["record_id"]))
            with pytest.raises(ValueError, match="different tenant"):
                await reconciliation_records.evaluate_reconciliation(
                    db, uuid.uuid4(), forecast_record=record,
                    g_comparison=_comparison(run_id=RUN1, recovered=[]))

    run(go)


def test_service_malformed_comparison_is_value_error(seed):
    async def go(sm):
        async with sm() as db:
            record = await db.get(ForecastRecord, UUID(seed["record_id"]))
            with pytest.raises(ValueError, match="'differences'"):
                await reconciliation_records.evaluate_reconciliation(
                    db, seed["tid"], forecast_record=record,
                    g_comparison={"run_id": RUN1, "drill": {"affected_ids": [], "recovered_ids": []}})

    run(go)


def test_service_list_counts_acknowledge(seed):
    _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]) + [EXTRA])
    _evaluate(seed, run_id=RUN2, recovered=_affected_ids(seed["forecast"]))

    async def go(sm):
        async with sm() as db:
            counts = await reconciliation_records.reconciliation_counts(db, seed["tid"])
            assert counts["total"] == 2
            assert counts["unacknowledged"] == 2
            assert counts["matches"] == 1
            assert counts["mismatches"] == 1
            assert counts["warning"] == 1
            assert counts["unacknowledged_critical"] == 0
            items = await reconciliation_records.list_reconciliations(db, seed["tid"])
            assert len(items) == 2
            assert items[0]["drill_run_id"] == RUN2  # newest first
            mismatches = await reconciliation_records.list_reconciliations(
                db, seed["tid"], severity="warning")
            assert len(mismatches) == 1
            assert mismatches[0]["drill_run_id"] == RUN1
            by_run = await reconciliation_records.list_reconciliations(
                db, seed["tid"], drill_run_id=UUID(RUN1))
            assert len(by_run) == 1
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            acked = await reconciliation_records.acknowledge_reconciliation(
                db, seed["tid"], UUID(items[0]["id"]), by=admin.id)
            assert acked["acknowledged"] is True
            assert acked["acknowledged_by"] == str(admin.id)
            assert acked["acknowledged_at"] is not None
            counts = await reconciliation_records.reconciliation_counts(db, seed["tid"])
            assert counts["unacknowledged"] == 1
            assert counts["acknowledged"] == 1
            missing = await reconciliation_records.get_reconciliation(
                db, seed["tid"], uuid.uuid4())
            assert missing is None

    run(go)


def test_service_prune_keeps_newest_and_drops_old(seed):
    _evaluate(seed, run_id=RUN1, recovered=_affected_ids(seed["forecast"]))
    _evaluate(seed, run_id=RUN2, recovered=_affected_ids(seed["forecast"]))
    _evaluate(seed, run_id=RUN3, recovered=_affected_ids(seed["forecast"]))

    async def go(sm):
        async with sm() as db:
            deleted = await reconciliation_records.prune_reconciliations(
                db, seed["tid"], keep=1)
            assert deleted == 2
            items = await reconciliation_records.list_reconciliations(db, seed["tid"])
            assert len(items) == 1
            assert items[0]["drill_run_id"] == RUN3
            deleted = await reconciliation_records.prune_reconciliations(
                db, seed["tid"], max_age_days=0,
                now=datetime(2026, 12, 5, tzinfo=UTC))
            assert deleted == 1

    run(go)


def test_service_redact_money_for_viewer():
    assert reconciliation_records.redact_money_for_viewer(None) is None
    summary = {"findings": [
        {"code": "forecasted_not_drilled", "money": {"PKR": 1500.0}},
        {"code": "drilled_not_forecasted", "money": None},
    ], "money_redacted": False}
    redacted = reconciliation_records.redact_money_for_viewer(summary)
    assert redacted["findings"][0]["money"] is None
    assert redacted["money_redacted"] is True
    assert summary["findings"][0]["money"] == {"PKR": 1500.0}  # input untouched


# -------------------------------------------------------------------------------- API

def _api_create(client, tenant_id, payload):
    return client.post(f"/api/predictive-continuity/{tenant_id}/reconciliations", json=payload)


def _mismatch_payload(seed):
    affected = _affected_ids(seed["forecast"])
    recovered = [eid for eid in affected if eid != IDS["pr-1"]]
    return {
        "forecast_record_id": seed["record_id"],
        "drill_comparison": _comparison(run_id=RUN1, recovered=recovered),
        "drill_kind": "resignation",
    }


def test_api_create_list_get_counts(client, seed):
    login(client, ADMIN_EMAIL)
    response = _api_create(client, seed["tenant_id"], _mismatch_payload(seed))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["matches"] is False
    assert body["counts"]["total"] == 1
    assert body["counts"]["critical"] == 1
    assert body["adjusted_drift_score"] == 10
    reconciliation = body["reconciliation"]
    assert reconciliation["findings"][0]["code"] == "forecasted_not_drilled"
    assert reconciliation["findings"][0]["money"] == {"PKR": 1500.0}
    assert reconciliation["money_redacted"] is False

    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations")
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/counts")
    assert response.json()["counts"]["mismatches"] == 1
    assert response.json()["counts"]["unacknowledged"] == 1
    rid = reconciliation["id"]
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/{rid}")
    assert response.status_code == 200, response.text
    assert response.json()["reconciliation"]["id"] == rid

    # Reconciling the same pair again is idempotent over HTTP too.
    response = _api_create(client, seed["tenant_id"], _mismatch_payload(seed))
    assert response.status_code == 200, response.text
    assert response.json()["created"] is False


def test_api_acknowledge_marks_seen(client, seed):
    login(client, ADMIN_EMAIL)
    created = _api_create(client, seed["tenant_id"], _mismatch_payload(seed)).json()
    rid = created["reconciliation"]["id"]
    response = client.post(
        f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/{rid}/acknowledge")
    assert response.status_code == 200, response.text
    assert response.json()["reconciliation"]["acknowledged"] is True
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/counts")
    assert response.json()["counts"]["unacknowledged"] == 0
    assert response.json()["counts"]["acknowledged"] == 1


def test_api_viewer_money_is_redacted(client, seed):
    login(client, ADMIN_EMAIL)
    created = _api_create(client, seed["tenant_id"], _mismatch_payload(seed)).json()
    rid = created["reconciliation"]["id"]
    assert created["reconciliation"]["findings"][0]["money"] == {"PKR": 1500.0}
    login(client, USER_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/{rid}")
    assert response.status_code == 200, response.text
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["findings"][0]["money"] is None
    assert reconciliation["money_redacted"] is True
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations")
    assert response.json()["reconciliations"][0]["findings"][0]["money"] is None


def test_api_member_without_read_gets_403(client, seed):
    login(client, USER2_EMAIL)
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations")
    assert response.status_code == 403, response.text
    response = _api_create(client, seed["tenant_id"], _mismatch_payload(seed))
    assert response.status_code == 403, response.text


def test_api_non_member_and_unknown_ids_get_404(client, seed):
    login(client, ADMIN_EMAIL)
    other = tenant(client, "Other Atlas")
    login(client, USER_EMAIL)
    response = client.get(f"/api/predictive-continuity/{other}/reconciliations")
    assert response.status_code == 404, response.text
    login(client, ADMIN_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations/{uuid.uuid4()}")
    assert response.status_code == 404, response.text
    payload = _mismatch_payload(seed)
    payload["forecast_record_id"] = str(uuid.uuid4())
    response = _api_create(client, seed["tenant_id"], payload)
    assert response.status_code == 404, response.text


def test_api_malformed_comparison_is_400(client, seed):
    login(client, ADMIN_EMAIL)
    payload = _mismatch_payload(seed)
    del payload["drill_comparison"]["differences"]
    response = _api_create(client, seed["tenant_id"], payload)
    assert response.status_code in (400, 422), response.text


def test_api_unauthenticated_gets_401(client, seed):
    # The seed fixture logs in as admin on the shared client; drop
    # the session cookie so this request is genuinely unauthenticated.
    client.cookies.clear()
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/reconciliations")
    assert response.status_code == 401, response.text


# -------------------------------------------------------------------------------- CLI

def _cli(*argv, env=None):
    merged = dict(os.environ)
    merged["DATABASE_URL"] = TEST_DATABASE_URL
    merged["TEST_DATABASE_URL"] = TEST_DATABASE_URL
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=merged)


def test_cli_help_lists_forecast_reconcile():
    result = _cli("forecast-reconcile", "--help")
    assert result.returncode == 0
    assert "--drill-comparison" in result.stdout
    assert "--dry-run" in result.stdout


def test_cli_unknown_record_exits_2(tmp_path):
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps(
        _comparison(run_id=RUN1, recovered=[])), encoding="utf-8")
    result = _cli("forecast-reconcile",
                  "--tenant-id", str(uuid.uuid4()),
                  "--forecast-record-id", str(uuid.uuid4()),
                  "--drill-comparison", str(comparison))
    assert result.returncode == 2
    assert "Unknown forecast record" in result.stderr


def test_cli_real_run_records_reconciliation(seed, tmp_path):
    affected = _affected_ids(seed["forecast"])
    recovered = [eid for eid in affected if eid != IDS["pr-1"]]
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps(
        _comparison(run_id=RUN1, recovered=recovered)), encoding="utf-8")
    args = ["forecast-reconcile", "--tenant-id", seed["tenant_id"],
            "--forecast-record-id", seed["record_id"],
            "--drill-comparison", str(comparison),
            "--drill-kind", "resignation"]
    dry = _cli(*args, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    dry_body = json.loads(dry.stdout)
    assert dry_body["dry_run"] is True
    assert dry_body["matches"] is False
    assert dry_body["counts"]["total"] == 1
    assert _reconciliation_count() == 0  # dry run writes nothing
    first = _cli(*args)
    assert first.returncode == 0, first.stderr
    body = json.loads(first.stdout)
    assert body["dry_run"] is False
    assert body["matches"] is False
    assert body["created"] is True
    assert body["adjusted_drift_score"] == 10
    assert _reconciliation_count() == 1
    assert _record_drift(seed) == 10
    second = _cli(*args)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["created"] is False
    assert _reconciliation_count() == 1
