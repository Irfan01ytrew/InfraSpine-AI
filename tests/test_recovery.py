"""Module 07 acceptance: real PostgreSQL, authenticated APIs, controlled HTTP faults."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.recovery import worker
from app.recovery.adapters import Context
from app.recovery.models import Command
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, TEST_DATABASE_URL, login, run_db
from tests.recovery_lab import Lab, LabAdapter


def uid(email):
    return str(run_db(lambda c: c.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": email})))


@pytest.fixture
def recovery_env(client, tmp_path):
    login(client, ADMIN_EMAIL)
    lab = Lab(tmp_path)
    adapter = LabAdapter(lab)
    client.app.state.recovery_registry.register("lab", adapter)
    tenant = client.post("/api/recovery/workspaces", json={"name": "Disposable QA"}).json()["id"]
    base = f"/api/recovery/{tenant}"
    for email, role in [(USER_EMAIL, "responder"), (USER2_EMAIL, "owner")]:
        assert client.put(base + "/members", json={"user_id": uid(email), "role": role}).status_code == 200
    target = client.post(base + "/targets", json={"name": "QA owner service", "owner_id": uid(ADMIN_EMAIL), "adapter": "lab", "resource": "lab"})
    assert target.status_code == 201, target.text
    env = {"client": client, "base": base, "tenant": tenant, "target": target.json(), "lab": lab, "adapter": adapter}
    yield env
    lab.close()


def event(env, **changes):
    payload = {"target_id": env["target"]["id"], "event_key": str(uuid.uuid4()), "source": "agent", "category": "timeout",
        "error_code": "tool_timeout", "title": "Agent diagnostic tool timed out", "occurred_at": datetime.now(UTC).isoformat(), "evidence": {"trace_id": "qa-trace"}}
    payload.update(changes)
    response = env["client"].post(env["base"] + "/events", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def detail(env, id):
    r = env["client"].get(env["base"] + f"/incidents/{id}")
    assert r.status_code == 200, r.text
    return r.json()


def proposal(env, *, action="retry", preapproved=True, attempts=3):
    incident = event(env)["incident_id"]
    c, base = env["client"], env["base"]
    assert c.post(base + f"/incidents/{incident}/diagnose").status_code == 200
    book = c.post(base + "/playbooks", json={"name": "Recover QA service", "adapter": "lab", "action": action, "preapproved": preapproved, "max_attempts": attempts, "backoff_seconds": 1})
    assert book.status_code == 201, book.text
    body = {"version": detail(env, incident)["incident"]["version"], "playbook_id": book.json()["id"], "request_key": str(uuid.uuid4())}
    cmd = c.post(base + f"/incidents/{incident}/recoveries", json=body)
    assert cmd.status_code == 202, cmd.text
    return incident, cmd.json(), body


def arun(env, callback):
    async def run():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, hide_parameters=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await callback(factory, env["client"].app.state.recovery_registry, env["client"].app.state.cipher)
        finally:
            await engine.dispose()
    return asyncio.run(run())


def due(env):
    # Advance only the disposable command's schedule, never application data.
    run_db(lambda c: c.execute(text("UPDATE recovery_commands SET available_at=now()-interval '1 second' WHERE tenant_id=:t"), {"t": uuid.UUID(env["tenant"])}))


def step(env):
    due(env)
    return arun(env, worker.process_once)


def test_detection_dedup_suppression_and_real_metrics(recovery_env):
    e = recovery_env
    suppressed = event(e, condition="expected_rejection", event_key="expected")
    assert suppressed["incident_id"] is None
    first = event(e, event_key="unique")
    duplicate = event(e, event_key="unique")
    assert duplicate["duplicate"] and first["event_id"] == duplicate["event_id"]
    for _ in range(3):
        assert event(e)["incident_id"] == first["incident_id"]
    d = detail(e, first["incident_id"])
    assert d["incident"]["occurrences"] == 4 and d["audit_valid"]
    metrics = e["client"].get(e["base"] + "/dashboard").json()["metrics"]
    assert metrics["active"] == 1 and metrics["mttr_seconds"] is None


def test_transient_threshold_avoids_false_positive(recovery_env):
    assert event(recovery_env, condition="transient")["incident_id"] is None
    assert event(recovery_env, condition="transient")["incident_id"]


def test_tenant_owner_and_observer_isolation(recovery_env):
    e = recovery_env
    iid = event(e)["incident_id"]
    login(e["client"], USER_EMAIL)
    assert e["client"].get(e["base"] + "/dashboard").json()["incidents"] == []
    assert e["client"].get(e["base"] + f"/incidents/{iid}").status_code == 404
    assert e["client"].get(e["base"] + f"/incidents/{iid}/report").status_code == 404
    assert e["client"].post(e["base"] + f"/incidents/{iid}/diagnose").status_code == 404
    login(e["client"], ADMIN_EMAIL)
    other = e["client"].post("/api/recovery/workspaces", json={"name": "Other tenant"}).json()["id"]
    assert e["client"].get(f"/api/recovery/{other}/incidents/{iid}").status_code == 404
    assert e["client"].put(e["base"] + "/members", json={"user_id": uid(USER_EMAIL), "role": "observer"}).status_code == 200
    login(e["client"], USER_EMAIL)
    assert e["client"].post(e["base"] + "/playbooks", json={"name": "Forbidden", "adapter": "lab", "action": "retry"}).status_code == 403


def test_recovery_lost_ack_verifies_before_retry_and_reopens(recovery_env):
    e = recovery_env
    e["adapter"].lose_ack = True
    iid, command, _ = proposal(e)
    assert step(e)
    assert detail(e, iid)["incident"]["status"] == "verifying"
    step(e)
    assert detail(e, iid)["incident"]["status"] == "verifying"
    step(e)
    d = detail(e, iid)
    assert d["incident"]["status"] == "resolved" and d["incident"]["verified_at"]
    assert d["commands"][0]["attempts"] == 1
    last_health = [t for t in d["timeline"] if t["kind"] == "health_observed"][-1]["data"]["evidence"]
    assert last_health["effects"] == 1 and last_health["completed_jobs"] == 3
    assert event(e)["incident_id"] == iid
    assert detail(e, iid)["incident"]["status"] == "reopened"


def test_human_approval_exact_action_separate_actor_and_fallback(recovery_env):
    e = recovery_env
    iid, cmd, _ = proposal(e, action="fallback", preapproved=False)
    assert not step(e)
    body = {"approve": True, "digest": cmd["action_digest"], "reason": "Approved disposable provider failover"}
    endpoint = e["base"] + f"/commands/{cmd['id']}/decision"
    assert e["client"].post(endpoint, json=body).status_code == 403
    login(e["client"], USER2_EMAIL)
    assert e["client"].post(endpoint, json={**body, "digest": "0"*64}).status_code == 409
    assert e["client"].post(endpoint, json=body).status_code == 200
    assert e["client"].post(endpoint, json=body).status_code == 409
    for _ in range(3): step(e)
    d = detail(e, iid)
    assert d["incident"]["status"] == "resolved"
    assert [x for x in d["timeline"] if x["kind"] == "health_observed"][-1]["data"]["evidence"]["provider"] == "backup"


def test_high_risk_never_preapproved(recovery_env):
    e = recovery_env
    r = e["client"].post(e["base"] + "/playbooks", json={"name": "Restart service", "adapter": "lab", "action": "restart", "preapproved": True})
    assert r.status_code == 422
    iid, cmd, _ = proposal(e, action="restart", preapproved=False)
    assert cmd["status"] == "awaiting_approval" and not step(e)


def test_revoked_requester_blocks_approved_execution(recovery_env):
    e = recovery_env
    iid, cmd, _ = proposal(e)
    login(e["client"], USER2_EMAIL)
    assert e["client"].put(e["base"] + "/members", json={"user_id": uid(ADMIN_EMAIL), "role": "owner", "active": False}).status_code == 200
    assert step(e)
    assert detail(e, iid)["commands"][0]["last_error"] == "authorization_invalid"


def test_parallel_claims_and_stale_worker_fencing(recovery_env):
    e = recovery_env
    iid, cmd, _ = proposal(e)
    async def claims(factory, registry, cipher):
        return await asyncio.gather(worker.claim(factory, registry, cipher), worker.claim(factory, registry, cipher))
    claimed = [x for x in arun(e, claims) if x]
    assert len(claimed) == 1
    old = claimed[0]
    run_db(lambda c: c.execute(text("UPDATE recovery_commands SET lease_until=now()-interval '1 second' WHERE id=:id"), {"id": uuid.UUID(cmd["id"])}))
    replacement = arun(e, worker.claim)
    assert replacement["phase"] == "verify"
    async def stale(factory, registry, cipher):
        return await worker.finish(factory, registry, cipher, old, {"forged": True}, None)
    assert arun(e, stale) is False
    assert detail(e, iid)["commands"][0]["attempts"] == 1


def test_retry_exhaustion_dead_letter_and_circuit(recovery_env):
    e = recovery_env
    e["lab"].fail_recovery = True
    iid, _, _ = proposal(e, attempts=2)
    for _ in range(10): step(e)
    d = detail(e, iid)
    assert d["incident"]["status"] == "escalated"
    assert d["commands"][0]["status"] == "dead_letter" and d["commands"][0]["attempts"] == 2
    assert not step(e)


def test_closure_requires_disposition_and_stale_version_rejected(recovery_env):
    e = recovery_env
    iid = event(e)["incident_id"]
    i = detail(e, iid)["incident"]
    url = e["base"] + f"/incidents/{iid}/transition"
    assert e["client"].post(url, json={"version": i["version"], "state": "resolved", "reason": "Looks okay"}).status_code == 422
    assert e["client"].post(url, json={"version": i["version"]-1, "state": "triaged", "reason": "Acknowledged"}).status_code == 409
    assert e["client"].post(url, json={"version": i["version"], "state": "resolved", "reason": "Injected false positive confirmed", "disposition": "false_positive"}).status_code == 200
    assert detail(e, iid)["incident"]["verified_at"] is None


def test_evidence_redaction_encryption_postmortem_and_db_guard(recovery_env):
    e = recovery_env
    iid = event(e, evidence={"api_key": "secret-qa-only", "message": "Bearer secret-qa-only", "nested": {"password": "secret-qa-only"}})["incident_id"]
    d = detail(e, iid)
    assert "secret-qa-only" not in json.dumps(d)
    for kind in ["postmortem", "preventive_action"]:
        assert e["client"].post(e["base"] + f"/incidents/{iid}/notes", json={"kind": kind, "note": "Attach owner and verified incident evidence to the follow-up task."}).status_code == 200
    r = e["client"].get(e["base"] + f"/incidents/{iid}/report")
    assert r.status_code == 200 and r.json()["report"]["preventive_actions"]
    assert r.json()["report"]["root_cause_confirmed"] is False
    with pytest.raises(Exception, match="append-only"):
        run_db(lambda c: c.execute(text("UPDATE recovery_timeline SET kind='forged' WHERE incident_id=:id"), {"id": uuid.UUID(iid)}))


def test_sla_and_expired_approval_escalation(recovery_env):
    e = recovery_env
    iid, cmd, _ = proposal(e, action="restart", preapproved=False)
    run_db(lambda c: c.execute(text("UPDATE recovery_incidents SET sla_due=now()-interval '1 second' WHERE id=:id"), {"id": uuid.UUID(iid)}))
    async def escalate(factory, registry, cipher): return await worker.escalate_due(factory, cipher)
    assert arun(e, escalate)
    d = detail(e, iid)
    assert d["incident"]["status"] == "escalated" and d["commands"][0]["status"] == "expired"


def test_idempotent_proposal_and_budget(recovery_env):
    e = recovery_env
    iid, cmd, body = proposal(e)
    r = e["client"].post(e["base"] + f"/incidents/{iid}/recoveries", json=body)
    assert r.status_code == 202 and r.json()["id"] == cmd["id"]
    run_db(lambda c: c.execute(text("UPDATE recovery_incidents SET recovery_count=10 WHERE id=:id"), {"id": uuid.UUID(iid)}))
    for _ in range(3): step(e)
    event(e)
    e["client"].post(e["base"] + f"/incidents/{iid}/diagnose")
    body.update(version=detail(e, iid)["incident"]["version"], request_key=str(uuid.uuid4()))
    assert e["client"].post(e["base"] + f"/incidents/{iid}/recoveries", json=body).status_code == 409


def test_scheduled_real_health_detection_and_unavailable_adapter(recovery_env):
    e = recovery_env
    assert arun(e, worker.monitor_once)
    run_db(lambda c: c.execute(text("UPDATE recovery_targets SET next_check=now()-interval '1 second' WHERE id=:id"), {"id": uuid.UUID(e["target"]["id"])}))
    assert arun(e, worker.monitor_once)
    d = e["client"].get(e["base"] + "/dashboard").json()
    assert d["metrics"]["active"] == 1 and d["targets"][0]["healthy"] is False
    r = e["client"].post(e["base"] + "/targets", json={"name": "Unconnected", "owner_id": uid(ADMIN_EMAIL), "adapter": "missing", "resource": "lab"})
    assert r.status_code == 422


def test_disable_playbook_fences_queued_dispatch(recovery_env):
    e = recovery_env
    iid, cmd, _ = proposal(e)
    assert e["client"].patch(e["base"] + f"/playbooks/{cmd['playbook_id']}", json={"enabled": False}).status_code == 200
    step(e)
    assert detail(e, iid)["commands"][0]["status"] == "dead_letter"
