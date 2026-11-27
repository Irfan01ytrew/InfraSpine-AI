"""Transactional ingestion, scoped analysis snapshots, and human review."""
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.services import audit
from . import access
from .mining import build_trace, discover
from .models import Baseline, Event, Job, Model, Process, Source, Trace

MAX_ANALYSIS_EVENTS = 100_000
MAX_CASE_EVENTS = 1000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def event_dict(row):
    return {"id": str(row.id), "source_id": str(row.source_id), "external_id": row.external_id, "case_id": row.case_id,
            "activity": row.activity, "occurred_at": row.occurred_at.astimezone(UTC).isoformat(),
            "finished_at": row.finished_at.astimezone(UTC).isoformat() if row.finished_at else None,
            "actor": row.actor, "department": row.department, "outcome": row.outcome,
            "terminal": row.terminal, "evidence_ref": row.evidence_ref}


async def enqueue(db, user, tenant_id, process_id):
    await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    # Process lock serializes ingestion and deduplicates pending work for this owner.
    job = await db.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.process_id == process_id, Job.owner_id == user.id, Job.status == "queued"))
    if job is None:
        job = Job(tenant_id=tenant_id, process_id=process_id, owner_id=user.id)
        db.add(job)
        await db.flush()
    return job


async def ingest(db, user, tenant_id, process_id, source_id, payload, ctx):
    process = await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    source = await access.source(db, user, tenant_id, process_id, source_id, write=True)
    now = datetime.now(UTC)
    prepared = {}
    for item in payload.events:
        if item.occurred_at > now + timedelta(minutes=5) or (item.finished_at and item.finished_at > now + timedelta(minutes=5)):
            raise InvalidInputError("Future event timestamps exceed the five-minute clock-skew allowance.")
        values = item.model_dump(mode="json")
        values["occurred_at"] = item.occurred_at.astimezone(UTC).isoformat()
        values["finished_at"] = item.finished_at.astimezone(UTC).isoformat() if item.finished_at else None
        hashed = digest(values)
        if item.external_id in prepared and prepared[item.external_id][1] != hashed:
            raise ConflictError("A batch contains conflicting records with the same event ID.")
        prepared[item.external_id] = (item, hashed)
    existing = {e.external_id: e.payload_hash for e in (await db.scalars(select(Event).where(Event.tenant_id == tenant_id, Event.source_id == source_id, Event.external_id.in_(prepared)))).all()}
    for external_id, (_, hashed) in prepared.items():
        if external_id in existing and existing[external_id] != hashed:
            raise ConflictError("An existing event ID has different content. Submit a separately identified correction; historical events are immutable.")
    new = [(item, hashed) for key, (item, hashed) in prepared.items() if key not in existing]
    replay = not new and source.cursor == payload.next_cursor
    if source.cursor != payload.expected_cursor and not replay:
        raise ConflictError("Synchronization cursor changed; reread the source before retrying.")
    affected = {item.case_id for item, _ in new}
    for item, hashed in new:
        db.add(Event(id=uuid4(), tenant_id=tenant_id, process_id=process_id, source_id=source_id, payload_hash=hashed, **item.model_dump()))
    await db.flush()
    if affected:
        rows = (await db.scalars(select(Event).where(Event.tenant_id == tenant_id, Event.source_id == source_id, Event.case_id.in_(affected)).order_by(Event.case_id, Event.occurred_at, Event.external_id))).all()
        grouped = {key: [] for key in affected}
        for row in rows:
            grouped[row.case_id].append(event_dict(row))
        if any(len(items) > MAX_CASE_EVENTS for items in grouped.values()):
            raise InvalidInputError(f"A case may contain at most {MAX_CASE_EVENTS} events. Split oversized cases at the source.")
        for key, items in grouped.items():
            data = build_trace(items)
            stmt = insert(Trace).values(id=uuid4(), tenant_id=tenant_id, process_id=process_id, source_id=source_id, case_id=key, data=data)
            await db.execute(stmt.on_conflict_do_update(index_elements=[Trace.source_id, Trace.case_id], set_={"data": data}))
        process.revision += 1
    if not replay:
        source.cursor = payload.next_cursor
    source.synced_at = now
    job = await enqueue(db, user, tenant_id, process_id) if new else None
    audit.record(db, ctx, "process.events_ingested", target=str(process_id), details={"tenant_id": str(tenant_id), "source_id": str(source_id), "inserted": len(new), "duplicate": len(payload.events)-len(new), "recalculated_cases": len(affected)})
    await db.commit()
    return {"inserted": len(new), "duplicates": len(payload.events)-len(new), "recalculated_cases": len(affected), "revision": process.revision, "cursor": source.cursor, "job_id": str(job.id) if job else None}


async def approved_baseline(db, tenant_id, process_id, source_ids):
    return await db.scalar(select(Baseline).where(Baseline.tenant_id == tenant_id, Baseline.process_id == process_id, Baseline.source_id.in_(source_ids), Baseline.status == "approved").order_by(Baseline.reviewed_at.desc(), Baseline.id).limit(1))


async def analyze(db, user, tenant_id, process_id, ctx):
    started = time.perf_counter()
    process = await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    visible = await access.sources(db, user, tenant_id, process_id)
    source_ids = sorted([str(s.id) for s in visible])
    count = await db.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id, Event.process_id == process_id, Event.source_id.in_([s.id for s in visible])))
    if count > MAX_ANALYSIS_EVENTS:
        raise InvalidInputError(f"This analysis scope exceeds {MAX_ANALYSIS_EVENTS:,} events. Partition the process before analysis.")
    baseline = await approved_baseline(db, tenant_id, process_id, [s.id for s in visible])
    input_hash = digest([process.revision, source_ids, str(baseline.id) if baseline else None])
    cached = await db.scalar(select(Model).where(Model.tenant_id == tenant_id, Model.process_id == process_id, Model.owner_id == user.id, Model.input_hash == input_hash))
    if cached:
        return cached
    traces = list((await db.scalars(select(Trace.data).where(Trace.tenant_id == tenant_id, Trace.process_id == process_id, Trace.source_id.in_([s.id for s in visible])).order_by(Trace.source_id, Trace.case_id))).all())
    prior = await db.scalar(select(Model).where(Model.tenant_id == tenant_id, Model.process_id == process_id, Model.owner_id == user.id, Model.source_ids == source_ids).order_by(Model.created_at.desc(), Model.id.desc()).limit(1))
    baseline_data = {**baseline.data, "id": str(baseline.id), "source_id": str(baseline.source_id), "reviewed_at": baseline.reviewed_at.isoformat()} if baseline else None
    data = await asyncio.to_thread(discover, traces, baseline_data, prior.data if prior else None)
    data["processing_ms"] = round((time.perf_counter()-started)*1000, 2)
    data["previous_model_id"] = str(prior.id) if prior else None
    data["sources"] = [{"id": str(s.id), "name": s.name, "kind": s.kind, "synced_at": s.synced_at.isoformat() if s.synced_at else None} for s in visible]
    data["confidence_threshold"] = .9
    data["needs_validation"] = data["summary"]["order_confidence"] < .9
    model = Model(tenant_id=tenant_id, process_id=process_id, owner_id=user.id, revision=process.revision, input_hash=input_hash, source_ids=source_ids, data=data)
    db.add(model)
    await db.flush()
    audit.record(db, ctx, "process.model_discovered", target=str(model.id), details={"tenant_id": str(tenant_id), "revision": process.revision, "events": count, "processing_ms": data["processing_ms"]})
    return model


async def get_model(db, user, tenant_id, process_id, model_id):
    await access.process(db, user, tenant_id, process_id)
    model = await db.scalar(select(Model).where(Model.id == model_id, Model.tenant_id == tenant_id, Model.process_id == process_id))
    if model is None:
        raise NotFoundError()
    allowed = {str(s.id) for s in await access.sources(db, user, tenant_id, process_id)}
    if not set(model.source_ids).issubset(allowed):
        raise NotFoundError()
    return model


def model_out(model, *, summary=False):
    result = {"id": str(model.id), "revision": model.revision, "created_at": model.created_at.isoformat(), "review_status": model.review_status}
    result["summary" if summary else "data"] = model.data["summary"] if summary else model.data
    return result


async def propose_baseline(db, user, tenant_id, process_id, payload, ctx, *, origin="human"):
    await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    await access.source(db, user, tenant_id, process_id, payload.source_id)
    data = payload.model_dump(mode="json", exclude={"source_id"})
    row = Baseline(tenant_id=tenant_id, process_id=process_id, source_id=payload.source_id, created_by=user.id, origin=origin, data=data)
    db.add(row)
    await db.flush()
    audit.record(db, ctx, "process.baseline_proposed", target=str(row.id), details={"tenant_id": str(tenant_id), "origin": origin})
    return row


async def review_baseline(db, user, tenant_id, process_id, baseline_id, decision, ctx):
    await access.membership(db, user, tenant_id, "steward")
    process = await access.process(db, user, tenant_id, process_id, lock=True)
    row = await db.scalar(select(Baseline).where(Baseline.tenant_id == tenant_id, Baseline.process_id == process_id, Baseline.id == baseline_id).with_for_update())
    if row is None:
        raise NotFoundError()
    await access.source(db, user, tenant_id, process_id, row.source_id)
    if row.created_by == user.id:
        raise PermissionDeniedError("A separate steward must review this proposed comparison baseline.")
    if row.status != "proposed":
        raise ConflictError("This baseline was already reviewed.")
    row.status, row.reviewed_by, row.reviewed_at = decision, user.id, datetime.now(UTC)
    process.revision += 1
    audit.record(db, ctx, "process.baseline_reviewed", target=str(row.id), details={"tenant_id": str(tenant_id), "decision": decision})
    await enqueue(db, user, tenant_id, process_id)
    await db.commit()
    return row


async def purge_expired(db, user, tenant_id, process_id, source_id, ctx):
    process = await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    source = await access.source(db, user, tenant_id, process_id, source_id, write=True)
    await access.membership(db, user, tenant_id, "steward")
    # Retain whole cases until their newest ingested event expires, avoiding partial reconstructed histories.
    cutoff = datetime.now(UTC) - timedelta(days=source.retention_days)
    cases = select(Event.case_id).where(Event.tenant_id == tenant_id, Event.source_id == source_id).group_by(Event.case_id).having(func.max(Event.created_at) < cutoff)
    keys = list((await db.scalars(cases)).all())
    if keys:
        await db.execute(delete(Event).where(Event.tenant_id == tenant_id, Event.source_id == source_id, Event.case_id.in_(keys)))
        await db.execute(delete(Trace).where(Trace.tenant_id == tenant_id, Trace.source_id == source_id, Trace.case_id.in_(keys)))
        # Cached evidence must expire too; no historical snapshot may retain purged source records.
        await db.execute(delete(Model).where(Model.tenant_id == tenant_id, Model.process_id == process_id, Model.source_ids.contains([str(source_id)])))
        process.revision += 1
    audit.record(db, ctx, "process.retention_applied", target=str(source_id), details={"tenant_id": str(tenant_id), "cases_removed": len(keys)})
    await db.commit()
    return {"cases_removed": len(keys)}
