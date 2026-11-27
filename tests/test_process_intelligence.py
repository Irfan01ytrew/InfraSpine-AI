import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.process_intelligence.models import Event, Job, Model
from app.process_intelligence.worker import claim_job, execute_claim, run_once
from tests.conftest import ADMIN_EMAIL, CSRF, TEST_DATABASE_URL, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.process_intelligence_dataset import BASELINE, purchase_events

ROOT = "/api/process-intelligence/v1"


def post(client, path, body=None, status=200):
    response = client.post(path, json=body, headers=CSRF)
    assert response.status_code == status, response.text
    return response.json()


def setup_process(client, *, with_events=True):
    login(client, ADMIN_EMAIL)
    tenant = post(client, ROOT + "/organizations", {"name": "Synthetic purchasing organization"}, 201)["id"]
    process = post(client, f"{ROOT}/{tenant}/processes", {"name": "Purchase approval"}, 201)["id"]
    path = f"{ROOT}/{tenant}/processes/{process}"
    source = post(client, path + "/sources", {"name": "Purchase ledger", "kind": "erp"}, 201)["id"]
    if with_events:
        post(client, path + f"/sources/{source}/events", {"events": purchase_events()})
    return tenant, path, source


def member(client, tenant, email, role="steward"):
    user_id = str(run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": email})))
    response = client.put(f"{ROOT}/{tenant}/members", json={"user_id": user_id, "role": role}, headers=CSRF)
    assert response.status_code == 200, response.text
    return user_id


def grant(client, path, source, user_id, allowed=True):
    response = client.put(path + f"/sources/{source}/access", json={"user_id": user_id, "allowed": allowed}, headers=CSRF)
    assert response.status_code == 200, response.text


def approved(client, tenant, path, source):
    reviewer = member(client, tenant, USER_EMAIL)
    grant(client, path, source, reviewer)
    baseline = post(client, path + "/baselines", {"source_id": source, **BASELINE}, 201)
    assert client.post(path + f'/baselines/{baseline["id"]}/review', json={"decision": "approved"}, headers=CSRF).status_code == 403
    login(client, USER_EMAIL)
    post(client, path + f'/baselines/{baseline["id"]}/review', {"decision": "approved"})
    login(client, ADMIN_EMAIL)
    return baseline


def test_purchase_discovery_sop_evidence_simulation_and_drift(client):
    tenant, path, source = setup_process(client)
    approved(client, tenant, path, source)
    model = post(client, path + "/analyze")
    data = model["data"]
    assert data["summary"]["events"] == 27
    assert data["summary"]["cases"] == 6
    assert data["summary"]["variants"] == 3
    assert data["summary"]["sla_breaches"] == 2
    assert data["summary"]["conformance_rate"] == .5
    assert data["summary"]["median_cycle_hours"] == 4.5
    slow = next(f for f in data["findings"] if f["kind"] == "bottleneck")
    assert slow["transition"] == ["Manager approval", "Director approval"]
    assert slow["hours"] == 25
    for finding in data["findings"]:
        assert finding["evidence_ids"]
        assert set(finding["evidence_ids"]).issubset(data["evidence"])
    evidence_id = slow["evidence_ids"][0]
    assert client.get(path + f"/evidence/{evidence_id}").json()["evidence_ref"].startswith("purchase-ledger:")
    simulation = post(client, path + "/simulate", {"model_id": model["id"], "actor": "manager", "absence_hours": 8})
    assert simulation["classification"] == "hypothetical"
    assert simulation["affected_cases"] == 6
    assert simulation["additional_sla_breaches"] == 4
    assert post(client, path + "/analyze")["id"] == model["id"]
    incoming = [e for e in purchase_events("drift", copies=4) if e["case_id"].endswith(("-3", "-4"))]
    result = post(client, path + f"/sources/{source}/events", {"events": incoming})
    assert result["recalculated_cases"] == 8
    second = post(client, path + "/analyze")
    assert second["data"]["drift"]["detected"] is True
    assert second["data"]["baseline"] == data["baseline"]
    assert len(client.get(path + "/models").json()["items"]) == 2
    exported = client.get(path + f'/models/{second["id"]}/export?format=csv')
    assert exported.status_code == 200
    assert "Purchase" not in exported.text  # real rows, no UI title masquerading as data
    assert "Director approval" in exported.text


def test_ingestion_idempotency_atomic_conflict_and_cursor(client):
    _, path, source = setup_process(client, with_events=False)
    records = purchase_events()
    batch = {"events": list(reversed(records)), "next_cursor": "page-1"}
    result = post(client, path + f"/sources/{source}/events", batch)
    assert result["inserted"] == 27
    assert post(client, path + f"/sources/{source}/events", batch)["duplicates"] == 27
    bad = copy.deepcopy(batch)
    bad["events"][0]["activity"] = "changed"
    assert client.post(path + f"/sources/{source}/events", json=bad, headers=CSRF).status_code == 409
    new = {"events": purchase_events("next"), "expected_cursor": "stale", "next_cursor": "page-2"}
    assert client.post(path + f"/sources/{source}/events", json=new, headers=CSRF).status_code == 409
    assert post(client, path + "/analyze")["data"]["summary"]["events"] == 27


def test_tenant_isolation_source_revocation_and_viewer_exports(client):
    tenant, path, source = setup_process(client)
    viewer = member(client, tenant, USER_EMAIL, "viewer")
    model = post(client, path + "/analyze")
    login(client, USER_EMAIL)
    assert client.get(path + "/models").status_code == 404
    login(client, ADMIN_EMAIL)
    grant(client, path, source, viewer)
    login(client, USER_EMAIL)
    assert client.get(path + f'/models/{model["id"]}').status_code == 200
    assert client.post(path + "/analyze", headers=CSRF).status_code == 403
    assert client.post(path + f"/sources/{source}/events", json={"events": purchase_events()}, headers=CSRF).status_code == 403
    assert client.get(path + f'/models/{model["id"]}/export').status_code == 200
    login(client, ADMIN_EMAIL)
    other = post(client, ROOT + "/organizations", {"name": "Other company"}, 201)["id"]
    assert client.get(path.replace(tenant, other) + f'/models/{model["id"]}').status_code == 404
    grant(client, path, source, viewer, False)
    login(client, USER_EMAIL)
    for suffix in (f'/models/{model["id"]}', f'/models/{model["id"]}/export', "/baselines", "/sources"):
        assert client.get(path + suffix).status_code == 404
    login(client, USER2_EMAIL)
    assert client.get(f"{ROOT}/{tenant}/processes").status_code == 404


def test_source_namespace_no_invented_cross_source_handoff(client):
    _, path, source = setup_process(client)
    second = post(client, path + "/sources", {"name": "Independent ledger", "kind": "event_log"}, 201)["id"]
    post(client, path + f"/sources/{second}/events", {"events": purchase_events()})
    model = post(client, path + "/analyze")
    assert model["data"]["summary"]["cases"] == 12
    assert model["data"]["summary"]["events"] == 54
    assert source != second


@pytest.mark.parametrize("mutation", ["naive", "negative", "future", "unknown", "oversize"])
def test_invalid_events_fail_closed(client, mutation):
    _, path, source = setup_process(client, with_events=False)
    event = purchase_events()[0]
    if mutation == "naive":
        event["occurred_at"] = "2026-01-01T00:00:00"
    elif mutation == "negative":
        event["finished_at"] = "2025-01-01T00:00:00Z"
    elif mutation == "future":
        event["occurred_at"] = event["finished_at"] = (datetime.now(UTC)+timedelta(days=2)).isoformat()
    elif mutation == "unknown":
        event["tenant_id"] = str(uuid4())
    else:
        event["case_id"] = "x" * 161
    assert client.post(path + f"/sources/{source}/events", json={"events": [event]}, headers=CSRF).status_code == 422
    assert post(client, path + "/analyze")["data"]["summary"]["events"] == 0


def test_ambiguous_order_open_cases_and_formula_export(client):
    _, path, source = setup_process(client, with_events=False)
    rows = purchase_events()[:4]
    rows[0]["case_id"] = rows[1]["case_id"] = rows[2]["case_id"] = rows[3]["case_id"] = "=HYPERLINK(1)"
    rows[1]["occurred_at"] = rows[1]["finished_at"] = rows[0]["occurred_at"]
    rows[-1]["terminal"] = False
    post(client, path + f"/sources/{source}/events", {"events": rows})
    model = post(client, path + "/analyze")
    assert model["data"]["summary"]["open_cases"] == 1
    assert model["data"]["summary"]["ambiguous_cases"] == 1
    assert model["data"]["edges"] == []
    assert "'=HYPERLINK" in client.get(path + f'/models/{model["id"]}/export?format=csv').text


def test_immutable_event_and_snapshot_database_guards(client):
    _, path, _ = setup_process(client)
    post(client, path + "/analyze")
    with pytest.raises(Exception, match="immutable"):
        run_db(lambda conn: conn.execute(text("UPDATE pi_events SET activity='tampered'")))
    with pytest.raises(Exception, match="immutable"):
        run_db(lambda conn: conn.execute(text("UPDATE pi_models SET data='{}'::jsonb")))


def test_durable_queue_fences_expired_claim_and_rechecks_membership(client):
    _, path, _ = setup_process(client)
    async def check():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            old = await claim_job(sm)
            async with sm() as db:
                job = await db.get(Job, old[0])
                job.lease_until = datetime.now(UTC)-timedelta(seconds=1)
                await db.commit()
            assert not await execute_claim(sm, *old)
            replacement = await claim_job(sm)
            assert replacement[1] != old[1]
            assert not await execute_claim(sm, *old)
            assert await execute_claim(sm, *replacement)
            async with sm() as db:
                job = await db.get(Job, old[0])
                assert job.status == "completed"
                assert len((await db.scalars(select(Model))).all()) == 1
        finally:
            await engine.dispose()
    asyncio.run(check())
    assert client.get(path + "/jobs").json()[0]["status"] == "completed"


def test_csrf_authentication_and_request_bound(client):
    login(client, ADMIN_EMAIL)
    client.headers.pop("X-Requested-With", None)
    assert client.post(ROOT + "/organizations", json={"name": "no csrf"}).status_code == 403
    response = client.post(ROOT + "/organizations", content=b"x" * (4*1024*1024+1), headers={**CSRF, "Content-Type": "application/json"})
    assert response.status_code == 413
    client.cookies.clear()
    assert client.get(ROOT + "/organizations").status_code == 401
