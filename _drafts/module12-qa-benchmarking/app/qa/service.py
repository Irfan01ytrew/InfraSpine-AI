"""Scoped queue and evidence transactions. API cannot supply results or executable code."""
import os
import uuid
from datetime import timedelta
from sqlalchemy import select, text, func
from fastapi import HTTPException
from app.services import audit
from .catalog import SUITES, catalog, requirements
from .domain import digest, release_report
from .models import QualityRun, QualityDefect, QualityDecision
from .source import source_identity

POLICY = {"version": 1, "max_age_hours": 24, "mandatory_unknown_blocks": True}

def tenant_id():
    try:
        return uuid.UUID(os.environ["QA_TENANT_ID"])
    except (KeyError, ValueError):
        raise HTTPException(503, "Operator must configure QA_TENANT_ID before using quality evidence") from None

def scope(model, tenant, owner):
    return (model.tenant_id == tenant, model.owner_id == owner)

async def lock_scope(db, tenant, owner):
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 12))"), {"scope": f"{tenant}:{owner}"})

def run_out(row):
    fields = ("id", "suite_id", "status", "source_digest", "revision", "manifest_sha256", "verifies", "created_at", "started_at", "finished_at", "results", "metrics", "provenance", "evidence_sha256", "rerun_of")
    return {key: value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, uuid.UUID) else value
            for key in fields for value in [getattr(row, key)]}

async def find_run(db, tenant, owner, run_id, *, locked=False):
    query = select(QualityRun).where(*scope(QualityRun, tenant, owner), QualityRun.id == run_id)
    row = await db.scalar(query.with_for_update() if locked else query)
    if row is None:
        raise HTTPException(404, "Quality run not found")
    return row

async def enqueue(db, tenant, owner, suite_id, ctx, rerun_of=None):
    if suite_id not in SUITES:
        raise HTTPException(422, "Unknown reviewed suite")
    await lock_scope(db, tenant, owner)
    if rerun_of:
        old = await find_run(db, tenant, owner, rerun_of)
        if old.suite_id != suite_id or old.status in {"queued", "running"}:
            raise HTTPException(409, "Only a terminal run of this suite can be rerun")
    count = await db.scalar(select(func.count()).select_from(QualityRun).where(*scope(QualityRun, tenant, owner), QualityRun.status.in_(["queued", "running"])))
    if count >= 3:
        raise HTTPException(429, "At most three pending runs per owner")
    run = QualityRun(tenant_id=tenant, owner_id=owner, suite_id=suite_id, **source_identity(),
                     verifies=SUITES[suite_id]["verifies"], rerun_of=rerun_of)
    db.add(run)
    await db.flush()
    audit.record(db, ctx, "qa.run.enqueue", target=str(run.id), details={"suite": suite_id, "source_digest": run.source_digest})
    await db.commit()
    return run_out(run)

async def cancel(db, tenant, owner, run_id, ctx):
    await lock_scope(db, tenant, owner)
    row = await find_run(db, tenant, owner, run_id, locked=True)
    # Running tests may own a disposable DB. Do not kill its parent during cleanup.
    if row.status != "queued":
        raise HTTPException(409, "Only queued runs can be cancelled; active runs have a bounded timeout")
    row.status, row.finished_at = "cancelled", await db.scalar(select(func.now()))
    audit.record(db, ctx, "qa.run.cancel", target=str(row.id))
    await db.commit()
    return run_out(row)

async def all_runs(db, tenant, owner):
    # Latest per suite is fetched independently: a long history never hides a release blocker.
    latest = (await db.scalars(select(QualityRun).where(*scope(QualityRun, tenant, owner))
              .distinct(QualityRun.suite_id).order_by(QualityRun.suite_id, QualityRun.created_at.desc(), QualityRun.id.desc()))).all()
    history = (await db.scalars(select(QualityRun).where(*scope(QualityRun, tenant, owner))
               .order_by(QualityRun.created_at.desc(), QualityRun.id.desc()).limit(100))).all()
    return list({r.id: r for r in [*history, *latest]}.values())

async def report(db, tenant, owner):
    return release_report(requirements(), [run_out(r) for r in await all_runs(db, tenant, owner)],
                          source_digest=source_identity()["source_digest"], policy=POLICY)

async def overview(db, tenant, owner):
    runs = [run_out(r) for r in await all_runs(db, tenant, owner)]
    result = release_report(requirements(), runs, source_digest=source_identity()["source_digest"], policy=POLICY)
    defects = (await db.scalars(select(QualityDefect).where(*scope(QualityDefect, tenant, owner)).order_by(QualityDefect.occurrences.desc()).limit(200))).all()
    decisions = (await db.scalars(select(QualityDecision).where(*scope(QualityDecision, tenant, owner)).order_by(QualityDecision.created_at.desc()).limit(20))).all()
    return {"report": result, "suites": catalog(), "runs": sorted(runs, key=lambda r: r["created_at"], reverse=True),
            "defects": [{k: str(getattr(d, k)) if k in {"id", "last_run_id"} else getattr(d, k)
                          for k in ("id", "test_id", "module", "severity", "status", "occurrences", "history", "last_run_id")} for d in defects],
            "decisions": [{"id": str(d.id), "decision": d.decision, "snapshot_sha256": d.snapshot_sha256, "created_at": d.created_at.isoformat()} for d in decisions],
            "history_limit": 100, "scope": "Requesting administrator within configured deployment tenant"}

async def signoff(db, tenant, owner, snapshot, ctx):
    await lock_scope(db, tenant, owner)
    current = await report(db, tenant, owner)
    if current["snapshot_sha256"] != snapshot:
        raise HTTPException(409, "Evidence changed; review the current release report")
    existing = await db.scalar(select(QualityDecision).where(*scope(QualityDecision, tenant, owner), QualityDecision.snapshot_sha256 == snapshot))
    if existing:
        return {"id": str(existing.id), "decision": existing.decision}
    decision = QualityDecision(tenant_id=tenant, owner_id=owner, snapshot_sha256=snapshot, decision=current["decision"], report=current)
    db.add(decision)
    await db.flush()
    audit.record(db, ctx, "qa.release.signoff", target=str(decision.id), details={"decision": current["decision"], "snapshot_sha256": snapshot})
    await db.commit()
    return {"id": str(decision.id), "decision": decision.decision}

async def claim(db, tenant, owner):
    await lock_scope(db, tenant, owner)
    now = await db.scalar(select(func.now()))
    expired = (await db.scalars(select(QualityRun).where(*scope(QualityRun, tenant, owner), QualityRun.status == "running", QualityRun.lease_until < now).with_for_update())).all()
    for row in expired:
        row.status, row.finished_at, row.claim_token = "blocked", now, None
        row.results = [{"test_id": "worker", "status": "blocked", "duration_ms": 0, "classification": "lease_expired", "message": "Worker interrupted. Review evidence and rerun explicitly."}]
        row.evidence_sha256 = digest(row.results)
    row = await db.scalar(select(QualityRun).where(*scope(QualityRun, tenant, owner), QualityRun.status == "queued")
                          .order_by(QualityRun.created_at).with_for_update(skip_locked=True).limit(1))
    if row:
        row.status, row.started_at, row.claim_token = "running", now, uuid.uuid4()
        row.lease_until = now + timedelta(seconds=SUITES[row.suite_id]["timeout"] + 240)
    await db.commit()
    return row

async def finish(db, tenant, owner, run_id, token, evidence):
    await lock_scope(db, tenant, owner)
    row = await find_run(db, tenant, owner, run_id, locked=True)
    now = await db.scalar(select(func.now()))
    if row.status != "running" or row.claim_token != token or row.lease_until < now:
        raise HTTPException(409, "Stale QA worker cannot overwrite evidence")
    row.status, row.finished_at = evidence["status"], now
    row.results, row.metrics, row.provenance = evidence["results"], evidence["metrics"], evidence["provenance"]
    row.evidence_sha256 = digest(evidence)
    for result in row.results:
        fingerprint = digest([row.suite_id, result["test_id"]])
        defect = await db.scalar(select(QualityDefect).where(*scope(QualityDefect, tenant, owner), QualityDefect.fingerprint == fingerprint).with_for_update())
        if result["status"] == "failed":
            if not defect:
                defect = QualityDefect(tenant_id=tenant, owner_id=owner, fingerprint=fingerprint, test_id=result["test_id"],
                                      module=SUITES[row.suite_id]["module"], severity="critical" if row.suite_id == "security" else "high",
                                      status="open", occurrences=0, history=[], last_run_id=row.id)
                db.add(defect)
            defect.occurrences += 1
            defect.status = "open"
        elif defect and result["status"] == "passed":
            defect.status = "verified_fix"
        if defect:
            defect.last_run_id = row.id
            defect.history = [*defect.history, {"run_id": str(row.id), "status": result["status"], "at": now.isoformat()}]
    audit.record(db, audit.AuditContext(owner, None, None, None), "qa.run.complete", target=str(row.id),
                 details={"status": row.status, "evidence_sha256": row.evidence_sha256})
    await db.commit()
    return run_out(row)
