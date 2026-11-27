"""Actual authenticated HTTP + PostgreSQL QA contracts, no external services."""
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login, run_db, USER_EMAIL, ADMIN_EMAIL
from app.qa import service
from app.qa.models import QualityRun, QualityDefect

BASE = "/api/admin/quality"
TENANT = uuid.UUID("00000000-0000-0000-0000-000000000012")

@pytest.fixture(autouse=True)
def qa_scope(monkeypatch):
    monkeypatch.setenv("QA_TENANT_ID", str(TENANT))

def enqueue(client, suite="qa-unit", **extra):
    response = client.post(BASE + "/runs", json={"suite_id": suite, **extra})
    assert response.status_code == 201, response.text
    return response.json()

def transact(fn):
    async def apply(conn):
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            return await fn(db)
    return run_db(apply)

async def owner(db):
    return await db.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": ADMIN_EMAIL})

def evidence(status="passed"):
    return {"status": status, "results": [{"test_id": "sample", "status": status, "duration_ms": 1,
             "classification": "assertion" if status == "failed" else "success", "message": ""}],
            "metrics": {"case_count": 1}, "provenance": {"environment": "synthetic-contract-fixture"}}

def complete(run_id, status="passed"):
    async def apply(db):
        row = await service.claim(db, TENANT, await owner(db))
        assert str(row.id) == run_id
        return await service.finish(db, TENANT, row.owner_id, row.id, row.claim_token, evidence(status))
    return transact(apply)

def test_api_rejects_anonymous_employee_csrf_and_scope_injection(client):
    assert client.get(BASE).status_code == 401
    login(client, USER_EMAIL)
    assert client.get(BASE).status_code == 403
    assert client.post(BASE + "/runs", json={"suite_id": "qa-unit"}).status_code == 403
    login(client)
    assert client.post(BASE + "/runs", json={"suite_id": "qa-unit"}, headers={"X-Requested-With": ""}).status_code == 403
    for extra in ({"tenant_id": str(uuid.uuid4())}, {"command": "anything"}, {"results": []}):
        assert client.post(BASE + "/runs", json={"suite_id": "qa-unit", **extra}).status_code == 422
    assert client.post(BASE + "/runs", json={"suite_id": "../../shell"}).status_code == 422

def test_missing_operator_tenant_fails_closed(client, monkeypatch):
    login(client)
    monkeypatch.delenv("QA_TENANT_ID")
    assert client.get(BASE).status_code == 503

def test_queue_persistence_completion_and_evidence_retrieval(client):
    login(client)
    run = enqueue(client)
    assert client.get(BASE + "/runs/" + run["id"]).json()["status"] == "queued"
    result = complete(run["id"])
    fetched = client.get(BASE + "/runs/" + run["id"]).json()
    assert fetched["status"] == "passed" and fetched["evidence_sha256"] == result["evidence_sha256"]
    assert client.get(BASE).json()["report"]["decision"] == "NO-GO"
    assert run_db(lambda c: c.scalar(text("SELECT count(*) FROM audit_logs WHERE action LIKE 'qa.%'"))) == 2

def test_owner_and_tenant_scope_on_every_lookup_and_dispatch(client, monkeypatch):
    login(client)
    first = enqueue(client)
    other = uuid.uuid4()
    monkeypatch.setenv("QA_TENANT_ID", str(other))
    assert client.get(BASE).json()["runs"] == []
    assert client.get(BASE + "/runs/" + first["id"]).status_code == 404
    assert client.post(BASE + "/runs/" + first["id"] + "/cancel").status_code == 404
    async def claim_other(db):
        return await service.claim(db, other, await owner(db))
    assert transact(claim_other) is None
    monkeypatch.setenv("QA_TENANT_ID", str(TENANT))
    run_db(lambda c: c.execute(text("UPDATE users SET role='admin' WHERE email=:email"), {"email": USER_EMAIL}))
    login(client, USER_EMAIL)
    assert client.get(BASE).json()["runs"] == []
    assert client.get(BASE + "/runs/" + first["id"]).status_code == 404
    assert client.post(BASE + "/runs", json={"suite_id": "qa-unit", "rerun_of": first["id"]}).status_code == 404

def test_queue_limit_cancel_and_rerun(client):
    login(client)
    runs = [enqueue(client) for _ in range(3)]
    assert client.post(BASE + "/runs", json={"suite_id": "qa-unit"}).status_code == 429
    assert client.post(BASE + "/runs/" + runs[0]["id"] + "/cancel").json()["status"] == "cancelled"
    rerun = enqueue(client, rerun_of=runs[0]["id"])
    assert rerun["rerun_of"] == runs[0]["id"]
    assert client.post(BASE + "/runs/" + runs[0]["id"] + "/cancel").status_code == 409

def test_expired_worker_retries_with_new_claim_and_deadletters_at_limit(client):
    login(client)
    first = enqueue(client)

    async def claim(db):
        return await service.claim(db, TENANT, await owner(db))

    claimed = transact(claim)
    assert claimed.attempt_count == 1
    run_db(lambda c: c.execute(text("UPDATE qa_runs SET lease_until=now()-interval '1 second' WHERE id=:id"), {"id": uuid.UUID(first["id"])}))

    async def finish_stale(db):
        return await service.finish(db, TENANT, await owner(db), claimed.id, claimed.claim_token, evidence())

    with pytest.raises(Exception, match="Stale QA worker"):
        transact(finish_stale)
    assert transact(claim) is None
    detail = client.get(BASE + "/runs/" + first["id"]).json()
    assert detail["status"] == "queued"
    assert detail["attempt_count"] == 1
    assert detail["next_attempt_at"] is not None
    assert detail["results"][0]["classification"] == "lease_expired"
    assert detail["attempt_history"][-1]["retry_scheduled"] is True
    run_db(lambda c: c.execute(text("UPDATE qa_runs SET next_attempt_at=now()-interval '1 second' WHERE id=:id"), {"id": uuid.UUID(first["id"])}))
    retry = transact(claim)
    assert retry is not None and retry.id == claimed.id
    assert retry.attempt_count == 2 and retry.claim_token != claimed.claim_token

    async def finish_current(db):
        return await service.finish(db, TENANT, await owner(db), retry.id, retry.claim_token, evidence("passed"))

    transact(finish_current)
    completed = client.get(BASE + "/runs/" + first["id"]).json()
    assert completed["status"] == "passed"
    assert [(entry["attempt"], entry["status"]) for entry in completed["attempt_history"]] == [
        (1, "running"), (1, "lease_expired"), (2, "running"), (2, "passed")
    ]

    capped = enqueue(client)
    for attempt in range(1, service.MAX_ATTEMPTS + 1):
        current = transact(claim)
        assert current is not None and current.attempt_count == attempt
        run_db(lambda c, run_id=uuid.UUID(capped["id"]): c.execute(
            text("UPDATE qa_runs SET lease_until=now()-interval '1 second' WHERE id=:id"), {"id": run_id}
        ))
        assert transact(claim) is None
        capped_state = client.get(BASE + "/runs/" + capped["id"]).json()
        if attempt < service.MAX_ATTEMPTS:
            assert capped_state["status"] == "queued"
            run_db(lambda c, run_id=uuid.UUID(capped["id"]): c.execute(
                text("UPDATE qa_runs SET next_attempt_at=now()-interval '1 second' WHERE id=:id"), {"id": run_id}
            ))
        else:
            assert capped_state["status"] == "blocked"
            assert capped_state["next_attempt_at"] is None
            assert [entry["status"] for entry in capped_state["attempt_history"]] == [
                "running", "lease_expired", "running", "lease_expired", "running", "lease_expired"
            ]

def test_wrong_claim_token_and_duplicate_completion_rejected(client):
    login(client)
    first = enqueue(client)
    async def claim(db):
        return await service.claim(db, TENANT, await owner(db))
    row = transact(claim)
    async def finish_with_token(db, token, result):
        return await service.finish(db, TENANT, await owner(db), row.id, token, result)
    with pytest.raises(Exception, match="Stale QA worker"):
        transact(lambda db: finish_with_token(db, uuid.uuid4(), evidence()))
    transact(lambda db: finish_with_token(db, row.claim_token, evidence()))
    with pytest.raises(Exception, match="Stale QA worker"):
        transact(lambda db: finish_with_token(db, row.claim_token, evidence("failed")))

def test_defect_deduplication_history_and_verified_rerun(client):
    login(client)
    first = enqueue(client)
    complete(first["id"], "failed")
    second = enqueue(client, rerun_of=first["id"])
    complete(second["id"], "failed")
    third = enqueue(client, rerun_of=second["id"])
    complete(third["id"], "passed")
    defects = client.get(BASE).json()["defects"]
    assert len(defects) == 1 and defects[0]["occurrences"] == 2
    assert defects[0]["status"] == "verified_fix"
    assert [h["status"] for h in defects[0]["history"]] == ["failed", "failed", "passed"]

def test_signoff_is_bound_to_reviewed_snapshot_and_cannot_override_no_go(client):
    login(client)
    report = client.get(BASE + "/report").json()
    assert report["decision"] == "NO-GO"
    payload = {"snapshot_sha256": report["snapshot_sha256"]}
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code == 200 and response.json()["decision"] == "NO-GO"
    assert client.post(BASE + "/signoff", json=payload).json() == response.json()
    assert client.post(BASE + "/signoff", json={**payload, "decision": "GO"}).status_code == 422
    enqueue(client)
    assert client.post(BASE + "/signoff", json=payload).status_code == 409

def test_terminal_evidence_cannot_be_rewritten_in_postgres(client):
    login(client)
    first = enqueue(client)
    complete(first["id"])
    with pytest.raises(Exception, match="immutable"):
        run_db(lambda c: c.execute(text("UPDATE qa_runs SET status='passed' WHERE id=:id"), {"id": uuid.UUID(first["id"])}))
