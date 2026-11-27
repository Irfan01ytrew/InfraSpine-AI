"""Append-only, hash-chained execution evidence; caller holds the run row lock."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from app.db.workflow_engine_models import WorkflowEvent, WorkflowNotice
from app.workflows.definition import digest


def event(db, run, kind, *, node_id=None, actor_id=None, detail=None):
    created = datetime.now(UTC)
    sequence = run.event_sequence + 1
    content = {'run_id': str(run.id), 'scope_id': str(run.scope_id), 'sequence': sequence,
               'kind': kind, 'node_id': node_id, 'actor_id': str(actor_id) if actor_id else None,
               'detail': detail or {}, 'created_at': created.isoformat(), 'previous_hash': run.event_hash}
    hash_value = digest(content)
    row = WorkflowEvent(id=uuid4(), run_id=run.id, scope_id=run.scope_id, sequence=sequence,
                        kind=kind, node_id=node_id, actor_id=actor_id, detail=detail or {},
                        previous_hash=run.event_hash, hash=hash_value, created_at=created)
    db.add(row)
    run.event_sequence, run.event_hash = sequence, hash_value
    return row


async def notify(db, step, recipient_id, kind):
    if recipient_id:
        await db.execute(insert(WorkflowNotice).values(
            id=uuid4(), scope_id=step.scope_id, step_id=step.id, recipient_id=recipient_id, kind=kind,
        ).on_conflict_do_nothing(index_elements=['step_id', 'recipient_id', 'kind']))


def verify_chain(rows):
    previous = '0' * 64
    for sequence, row in enumerate(rows, 1):
        content = {'run_id': str(row.run_id), 'scope_id': str(row.scope_id), 'sequence': row.sequence,
                   'kind': row.kind, 'node_id': row.node_id, 'actor_id': str(row.actor_id) if row.actor_id else None,
                   'detail': row.detail, 'created_at': row.created_at.isoformat(), 'previous_hash': row.previous_hash}
        if row.sequence != sequence or row.previous_hash != previous or digest(content) != row.hash:
            return False
        previous = row.hash
    return True
