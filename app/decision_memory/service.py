"""Step M (slice 2): persistence for decision records.

The service owns the ``decision_records`` / ``decision_events`` tables and
is the only writer of them. It reuses slice 1's pure validation
(``app.decision_memory.records``) instead of re-implementing it, and it
routes every privileged step through ``app/platform``:

- every call starts with ``require()`` — 404 for non-members, 403 for
  members without the permission (deny by default; the legacy admin role
  implies nothing here);
- sensitive kinds (``finance``, ``supplier``, ``people``) need a consumed
  platform approval bound to the exact action before the record becomes
  active — the approval is requested first (see
  ``request_decision_approval``) by one person and decided by another
  (``decide_approval`` refuses self-decision);
- one audit row per lifecycle event (``decisions.recorded``,
  ``decisions.superseded``, ``decisions.revoked``) plus one row in the
  append-only ``decision_events`` table.

Decisions are records, never permissions: recording, citing, superseding
or revoking a decision never grants access, never changes an approval,
never moves money and never changes a payee. History is append-only —
there is no update-in-place and no delete in this module. Supersession
and revocation change only status columns; the old record stays readable.

Evidence is stored as references only (kind + ref + label), never copied
text. Read-time filtering lives in ``get_decision``: the caller passes
the set of ``(kind, ref)`` pairs the reader may read (slice 3 computes
it per evidence kind); anything not in the set is dropped and counted,
deny by default.

Nothing here commits except ``request_decision_approval`` (``request_approval``
commits by design); the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.decision_models import DecisionEventRow, DecisionRecordRow
from app.db.models import User
from app.decision_memory.records import (
    DecisionRecord,
    EvidenceRef,
    decision_fingerprint,
    flag_suspicious,
    new_decision_id,
    supersede_record,
    validate_decision,
    validate_graph,
)
from app.platform import access as platform_access
from app.platform.access import Principal
from app.platform.approvals import (
    ActionSpec,
    ApprovalGrant,
    consume_approval,
    request_approval,
)
from app.platform.events import audit

__all__ = [
    "AUDIT_RECORDED",
    "AUDIT_REVIEWED",
    "AUDIT_REVOKED",
    "AUDIT_SUPERSEDED",
    "DEFAULT_REVIEW_DAYS",
    "PERMISSION_APPROVE",
    "PERMISSION_READ",
    "PERMISSION_RECORD",
    "SENSITIVE_KINDS",
    "DecisionView",
    "decision_action_spec",
    "decision_dependencies",
    "decision_view",
    "get_decision",
    "list_decisions",
    "record_decision",
    "request_decision_approval",
    "revoke_decision",
    "search_decisions",
    "supersede_decision",
    "verify_fingerprint",
]

PERMISSION_RECORD = "decision.record"
PERMISSION_READ = "decision.read"
PERMISSION_APPROVE = "decision.approve"

# Kinds whose records need an owner approval before they become active
# (Step M plan §6, owner decision 1).
SENSITIVE_KINDS = frozenset({"finance", "supplier", "people"})

# Default review date: decided date + 180 days (Step M plan §6, owner decision 3).
DEFAULT_REVIEW_DAYS = 180

AUDIT_RECORDED = "decisions.recorded"
AUDIT_SUPERSEDED = "decisions.superseded"
AUDIT_REVOKED = "decisions.revoked"
AUDIT_REVIEWED = "decisions.reviewed"


@dataclass(frozen=True)
class DecisionView:
    """A decision as one reader may see it: the record, the evidence they
    may read, and how many evidence references were hidden from them."""

    record: DecisionRecord
    visible_evidence: tuple[EvidenceRef, ...]
    hidden_evidence: int
    row: DecisionRecordRow


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_decision_id(decision_id: str | None) -> tuple[uuid.UUID, str]:
    """Parse a decision id to (UUID, canonical dashed string).

    A caller-supplied id must be a UUID (``new_decision_id()`` returns
    uuid4 hex, which parses); anything else is rejected so the pure
    record id and the database primary key can never drift apart.
    """
    if decision_id is None:
        row_id = uuid.uuid4()
        return row_id, str(row_id)
    try:
        row_id = uuid.UUID(decision_id)
    except (ValueError, AttributeError, TypeError):
        raise InvalidInputError("decision_id must be a UUID.") from None
    return row_id, str(row_id)


def decision_action_spec(*, action_type: Literal["decision.record", "decision.revoke", "decision.expire"],
                         scope_id: uuid.UUID, decision_id: str, kind: str) -> ActionSpec:
    """The exact action an approval must cover for a sensitive decision."""
    _, canonical_id = _normalize_decision_id(decision_id)
    return ActionSpec(type=action_type, scope_id=scope_id,
                      params={"decision_id": canonical_id, "kind": kind},
                      subject=f"decision:{canonical_id}")


def supersede_action_spec(*, scope_id: uuid.UUID, decision_id: str, superseded_id: str, kind: str) -> ActionSpec:
    """The exact action an approval must cover for a sensitive supersession."""
    _, new_id = _normalize_decision_id(decision_id)
    _, old_id = _normalize_decision_id(superseded_id)
    return ActionSpec(
        type="decision.supersede", scope_id=scope_id,
        params={"decision_id": new_id, "supersedes": old_id, "kind": kind},
        subject=f"decision:{old_id}",
    )


async def request_decision_approval(db: AsyncSession, actor: Principal, *,
                                    action_type: Literal["decision.record", "decision.supersede", "decision.revoke", "decision.expire"],
                                    scope_id: uuid.UUID, decision_id: str, kind: str,
                                    superseded_id: str | None = None,
                                    ttl: timedelta | None = None):
    """Request the owner approval a sensitive decision needs.

    The approver must hold ``decision.approve`` and must not be the
    requester (enforced by ``decide_approval``). Commits, like
    ``request_approval`` — call it before opening the record transaction,
    then pass the returned request's id as ``approval_id``.
    """
    if action_type == "decision.supersede":
        if superseded_id is None:
            raise InvalidInputError("superseded_id is required for a supersede approval.")
        spec = supersede_action_spec(scope_id=scope_id, decision_id=decision_id,
                                     superseded_id=superseded_id, kind=kind)
    else:
        spec = decision_action_spec(action_type=action_type, scope_id=scope_id,
                                    decision_id=decision_id, kind=kind)
    kwargs: dict = {}
    if ttl is not None:
        kwargs["ttl"] = ttl
    return await request_approval(db, actor, spec, required_permission=PERMISSION_APPROVE, **kwargs)


# ----------------------------------------------------------------------------------------------- mapping

def _row_to_pure(row: DecisionRecordRow) -> DecisionRecord:
    return DecisionRecord(
        id=str(row.id),
        scope_id=str(row.scope_id),
        title=row.title,
        rationale=row.rationale,
        kind=row.kind,
        status=row.status,
        decided_by=str(row.decided_by),
        decided_at=row.decided_at,
        alternatives_considered=tuple(row.alternatives_considered or ()),
        evidence=tuple(
            EvidenceRef(kind=item["kind"], ref=item["ref"], label=item["label"])
            for item in (row.evidence or ())
        ),
        supersedes=tuple(row.supersedes or ()),
        depends_on=tuple(row.depends_on or ()),
        review_at=row.review_at,
        superseded_by=str(row.superseded_by) if row.superseded_by else None,
        superseded_at=row.superseded_at,
        needs_review=bool(row.needs_review),
    )


def _pure_to_row(record: DecisionRecord, *, row_id: uuid.UUID, decided_by: uuid.UUID) -> DecisionRecordRow:
    return DecisionRecordRow(
        id=row_id,
        scope_id=uuid.UUID(record.scope_id),
        title=record.title,
        rationale=record.rationale,
        kind=record.kind,
        status=record.status,
        decided_by=decided_by,
        decided_at=record.decided_at,
        alternatives_considered=list(record.alternatives_considered),
        evidence=[{"kind": ref.kind, "ref": ref.ref, "label": ref.label} for ref in record.evidence],
        supersedes=list(record.supersedes),
        depends_on=list(record.depends_on),
        review_at=record.review_at.date() if isinstance(record.review_at, datetime) else record.review_at,
        superseded_by=uuid.UUID(record.superseded_by) if record.superseded_by else None,
        superseded_at=record.superseded_at,
        needs_review=record.needs_review,
        fingerprint=decision_fingerprint(record),
    )


async def _load_scope_pure(db: AsyncSession, scope_id: uuid.UUID) -> list[DecisionRecord]:
    rows = await db.scalars(
        select(DecisionRecordRow).where(DecisionRecordRow.scope_id == scope_id))
    return [_row_to_pure(row) for row in rows]


def _validate_or_raise(records: list[DecisionRecord]) -> None:
    errors = validate_graph(records)
    if errors:
        raise InvalidInputError("Invalid decision graph: " + "; ".join(errors))


async def _get_scoped_row(db: AsyncSession, scope_id: uuid.UUID, decision_id: str) -> DecisionRecordRow:
    """Load a decision row, or 404 — including when it exists in another scope."""
    row_id, _ = _normalize_decision_id(decision_id)
    row = await db.get(DecisionRecordRow, row_id)
    if row is None or row.scope_id != scope_id:
        raise NotFoundError("Not found.")
    return row


def _append_event(db: AsyncSession, *, decision_id: uuid.UUID, event: str, actor: Principal,
                  reason: str = "", details: dict | None = None) -> None:
    db.add(DecisionEventRow(
        decision_id=decision_id, event=event, actor_id=actor.user_id,
        reason=" ".join(str(reason or "").split())[:500],
        details=details or {},
    ))


def _audit(db: AsyncSession, actor: Principal, action: str, *, target: str, details: dict | None = None) -> None:
    audit(db, actor, action, target=target, details=details or {})


async def _consume_sensitive_approval(db: AsyncSession, *, spec: ActionSpec,
                                      approval_id: uuid.UUID | None) -> ApprovalGrant | None:
    """Consume the approval a sensitive decision needs, or refuse.

    Non-sensitive kinds need no approval and return None. A missing or
    unusable approval for a sensitive kind is refused — the record is
    never created without it.
    """
    if approval_id is None:
        raise PermissionDeniedError(
            "Sensitive decisions need an owner approval first. Request one for this exact action, "
            "have someone else approve it, then retry with the approval id.")
    return await consume_approval(db, approval_id, spec)


async def _active_dependents(db: AsyncSession, scope_id: uuid.UUID, decision_id: str) -> list[DecisionRecordRow]:
    """Active decisions in this scope that depend on the given decision."""
    rows = await db.scalars(
        select(DecisionRecordRow).where(
            DecisionRecordRow.scope_id == scope_id,
            DecisionRecordRow.status == "active",
            DecisionRecordRow.depends_on.contains([decision_id]),
        ))
    return list(rows)


# ----------------------------------------------------------------------------------------------- record

async def record_decision(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                          title: str, rationale: str, kind: str = "other",
                          decided_by: uuid.UUID | None = None, decided_at: datetime | None = None,
                          alternatives_considered: tuple[str, ...] = (),
                          evidence: tuple[EvidenceRef, ...] = (),
                          depends_on: tuple[str, ...] = (),
                          review_at: date | datetime | None = None,
                          decision_id: str | None = None,
                          approval_id: uuid.UUID | None = None) -> DecisionRecordRow:
    """Record a new decision in a scope. The caller needs ``decision.record``.

    Supersession is not done here — use ``supersede_decision``, which
    marks the old decision superseded in the same transaction. ``depends_on``
    targets must already exist and be active in the same scope.
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    row_id, canonical_id = _normalize_decision_id(decision_id)
    decided_by_id = decided_by or actor.user_id
    if await db.get(User, decided_by_id) is None:
        raise NotFoundError("Decider not found.")
    decided = decided_at or _now()
    if review_at is None:
        review_at = (decided.date() + timedelta(days=DEFAULT_REVIEW_DAYS))
    if isinstance(review_at, datetime):
        review_at = review_at.date()
    record = DecisionRecord(
        id=canonical_id, scope_id=str(scope_id), title=title, rationale=rationale, kind=kind,
        status="active", decided_by=str(decided_by_id), decided_at=decided,
        alternatives_considered=tuple(alternatives_considered),
        evidence=tuple(evidence), supersedes=(), depends_on=tuple(depends_on),
        review_at=review_at, needs_review=False,
    )
    record = replace(record, needs_review=flag_suspicious(record))
    errors = validate_decision(record)
    if errors:
        raise InvalidInputError("Invalid decision: " + "; ".join(errors))
    if kind in SENSITIVE_KINDS:
        spec = decision_action_spec(action_type="decision.record", scope_id=scope_id,
                                    decision_id=canonical_id, kind=kind)
        await _consume_sensitive_approval(db, spec=spec, approval_id=approval_id)
    scope_records = await _load_scope_pure(db, scope_id)
    _validate_or_raise([*scope_records, record])
    row = _pure_to_row(record, row_id=row_id, decided_by=decided_by_id)
    db.add(row)
    await db.flush()
    _append_event(db, decision_id=row.id, event="recorded", actor=actor,
                  details={"kind": kind, "fingerprint": row.fingerprint})
    _audit(db, actor, AUDIT_RECORDED, target=str(row.id),
           details={"kind": kind, "decided_by": str(decided_by_id), "fingerprint": row.fingerprint})
    return row


async def supersede_decision(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                             superseded_id: str, title: str, rationale: str, kind: str = "other",
                             decided_by: uuid.UUID | None = None, decided_at: datetime | None = None,
                             alternatives_considered: tuple[str, ...] = (),
                             evidence: tuple[EvidenceRef, ...] = (),
                             depends_on: tuple[str, ...] = (),
                             review_at: date | datetime | None = None,
                             decision_id: str | None = None,
                             approval_id: uuid.UUID | None = None,
                             ) -> tuple[DecisionRecordRow, DecisionRecordRow]:
    """Supersede an active decision with a new one, atomically.

    Returns ``(new_row, old_row)``. The old decision must be active and
    must have no active dependents (re-point them first — the graph must
    stay valid). A sensitive kind — the new record's or the old one's —
    needs a consumed approval for this exact supersession.
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    old_row = await _get_scoped_row(db, scope_id, superseded_id)
    if old_row.status != "active":
        raise InvalidInputError(f"Only an active decision can be superseded, got {old_row.status!r}.")
    dependents = await _active_dependents(db, scope_id, str(old_row.id))
    if dependents:
        names = ", ".join(f"{row.title!r}" for row in dependents)
        raise InvalidInputError(
            f"Cannot supersede: active decisions depend on this one ({names}). "
            "Supersede or revoke them first.")
    row_id, canonical_id = _normalize_decision_id(decision_id)
    decided_by_id = decided_by or actor.user_id
    if await db.get(User, decided_by_id) is None:
        raise NotFoundError("Decider not found.")
    decided = decided_at or _now()
    if review_at is None:
        review_at = (decided.date() + timedelta(days=DEFAULT_REVIEW_DAYS))
    if isinstance(review_at, datetime):
        review_at = review_at.date()
    new_record = DecisionRecord(
        id=canonical_id, scope_id=str(scope_id), title=title, rationale=rationale, kind=kind,
        status="active", decided_by=str(decided_by_id), decided_at=decided,
        alternatives_considered=tuple(alternatives_considered),
        evidence=tuple(evidence), supersedes=(str(old_row.id),), depends_on=tuple(depends_on),
        review_at=review_at, needs_review=False,
    )
    new_record = replace(new_record, needs_review=flag_suspicious(new_record))
    errors = validate_decision(new_record)
    if errors:
        raise InvalidInputError("Invalid decision: " + "; ".join(errors))
    if kind in SENSITIVE_KINDS or old_row.kind in SENSITIVE_KINDS:
        spec = supersede_action_spec(scope_id=scope_id, decision_id=canonical_id,
                                     superseded_id=str(old_row.id), kind=kind)
        await _consume_sensitive_approval(db, spec=spec, approval_id=approval_id)
    old_pure = _row_to_pure(old_row)
    try:
        superseded_pure = supersede_record(old_pure, superseding_id=canonical_id, at=decided)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    scope_records = await _load_scope_pure(db, scope_id)
    combined = [superseded_pure if item.id == old_pure.id else item for item in scope_records]
    _validate_or_raise([*combined, new_record])
    new_row = _pure_to_row(new_record, row_id=row_id, decided_by=decided_by_id)
    db.add(new_row)
    # Flush the new record first: the old row's superseded_by points at it,
    # and the flush would otherwise emit the UPDATE before the INSERT and
    # trip the foreign key.
    await db.flush()
    old_row.status = "superseded"
    old_row.superseded_by = row_id
    old_row.superseded_at = decided
    old_row.updated_at = decided
    await db.flush()
    _append_event(db, decision_id=new_row.id, event="recorded", actor=actor,
                  details={"kind": kind, "fingerprint": new_row.fingerprint,
                           "supersedes": str(old_row.id)})
    _append_event(db, decision_id=old_row.id, event="superseded", actor=actor,
                  details={"superseded_by": str(new_row.id)})
    _audit(db, actor, AUDIT_RECORDED, target=str(new_row.id),
           details={"kind": kind, "decided_by": str(decided_by_id),
                    "fingerprint": new_row.fingerprint, "supersedes": str(old_row.id)})
    _audit(db, actor, AUDIT_SUPERSEDED, target=str(old_row.id),
           details={"superseded_by": str(new_row.id)})
    return new_row, old_row


async def revoke_decision(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                          decision_id: str, reason: str,
                          approval_id: uuid.UUID | None = None) -> DecisionRecordRow:
    """Revoke an active decision. It stays readable with status ``revoked``.

    Refuses when active decisions depend on it (revoke them first) and,
    for sensitive kinds, without a consumed approval for this exact
    revocation.
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    row = await _get_scoped_row(db, scope_id, decision_id)
    if row.status != "active":
        raise InvalidInputError(f"Only an active decision can be revoked, got {row.status!r}.")
    clean_reason = " ".join(str(reason or "").split())
    if not 1 <= len(clean_reason) <= 500:
        raise InvalidInputError("A revocation reason of 1–500 characters is required.")
    dependents = await _active_dependents(db, scope_id, str(row.id))
    if dependents:
        names = ", ".join(f"{item.title!r}" for item in dependents)
        raise InvalidInputError(
            f"Cannot revoke: active decisions depend on this one ({names}). "
            "Supersede or revoke them first.")
    if row.kind in SENSITIVE_KINDS:
        spec = decision_action_spec(action_type="decision.revoke", scope_id=scope_id,
                                    decision_id=str(row.id), kind=row.kind)
        await _consume_sensitive_approval(db, spec=spec, approval_id=approval_id)
    row.status = "revoked"
    row.updated_at = _now()
    await db.flush()
    _append_event(db, decision_id=row.id, event="revoked", actor=actor,
                  reason=clean_reason, details={"kind": row.kind})
    _audit(db, actor, AUDIT_REVOKED, target=str(row.id),
           details={"kind": row.kind, "reason": clean_reason})
    return row


# ----------------------------------------------------------------------------------------------- reads

async def get_decision(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID, decision_id: str,
                       readable_evidence: frozenset[tuple[str, str]] = frozenset()) -> DecisionView:
    """One decision as this reader may see it. Needs ``decision.read``.

    ``readable_evidence`` is the set of ``(kind, ref)`` pairs the reader
    may read (slice 3 computes it per evidence kind); anything else is
    dropped and counted — deny by default.
    """
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    row = await _get_scoped_row(db, scope_id, decision_id)
    return decision_view(row, readable_evidence)


def decision_view(row: DecisionRecordRow,
                  readable_evidence: frozenset[tuple[str, str]] = frozenset()) -> DecisionView:
    """A stored row as one reader may see it (pure mapping, no DB access).

    ``readable_evidence`` is the set of ``(kind, ref)`` pairs the reader
    may read; anything else is dropped and counted — deny by default.
    """
    from app.decision_memory.records import visible_evidence

    record = _row_to_pure(row)
    visible, hidden = visible_evidence(record, set(readable_evidence))
    return DecisionView(record=record, visible_evidence=tuple(visible), hidden_evidence=hidden, row=row)


async def search_decisions(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID, subject: str,
                           include_archived: bool = False, limit: int = 50) -> list[DecisionRecordRow]:
    """Decisions whose title or rationale mentions ``subject`` ("why" query).

    Needs ``decision.read``. The subject is untrusted text used only as
    a bound LIKE parameter (``%``/``_``/``\\`` escaped), never
    interpreted. Newest first.
    """
    from sqlalchemy import or_

    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    clean = " ".join(str(subject or "").split())
    if not 1 <= len(clean) <= 200:
        raise InvalidInputError("A search subject of 1–200 characters is required.")
    escaped = clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    query = select(DecisionRecordRow).where(
        DecisionRecordRow.scope_id == scope_id,
        or_(DecisionRecordRow.title.ilike(pattern, escape="\\"),
            DecisionRecordRow.rationale.ilike(pattern, escape="\\")),
    )
    if not include_archived:
        query = query.where(DecisionRecordRow.status == "active")
    query = query.order_by(DecisionRecordRow.decided_at.desc(),
                           DecisionRecordRow.created_at.desc(), DecisionRecordRow.id.desc())
    return list(await db.scalars(query.limit(max(1, min(limit, 200)))))


async def decision_dependencies(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                                decision_id: str) -> dict:
    """The dependency view for one decision. Needs ``decision.read``.

    Returns the decision's row plus the rows it depends on, the rows
    that depend on it, the rows it supersedes, and the row that
    superseded it (if any) — all in the same scope. No delete anywhere;
    superseded/revoked rows stay readable here.
    """
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    row = await _get_scoped_row(db, scope_id, decision_id)
    record = _row_to_pure(row)

    async def _rows(ids: list[str]) -> list[DecisionRecordRow]:
        out: list[DecisionRecordRow] = []
        for target in ids:
            try:
                out.append(await _get_scoped_row(db, scope_id, target))
            except NotFoundError:
                continue
        return out

    depended_on_by = list(await db.scalars(
        select(DecisionRecordRow).where(
            DecisionRecordRow.scope_id == scope_id,
            DecisionRecordRow.depends_on.contains([str(row.id)]),
        ).order_by(DecisionRecordRow.decided_at.desc())))
    superseded_by = None
    if row.superseded_by is not None:
        superseded_by = await db.get(DecisionRecordRow, row.superseded_by)
        if superseded_by is not None and superseded_by.scope_id != scope_id:
            superseded_by = None
    return {
        "record": row,
        "depends_on": await _rows(list(record.depends_on)),
        "depended_on_by": depended_on_by,
        "supersedes": await _rows(list(record.supersedes)),
        "superseded_by": superseded_by,
    }


async def list_decisions(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                         kind: str | None = None, status: str | None = None,
                         decided_by: uuid.UUID | None = None,
                         decided_after: datetime | None = None,
                         decided_before: datetime | None = None,
                         include_archived: bool = False,
                         limit: int = 100) -> list[DecisionRecordRow]:
    """Decisions in a scope, newest first. Needs ``decision.read``.

    By default only active decisions are listed — superseded and revoked
    records are archived from lists (never deleted) unless
    ``include_archived`` is set.
    """
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    query = select(DecisionRecordRow).where(DecisionRecordRow.scope_id == scope_id)
    if not include_archived:
        query = query.where(DecisionRecordRow.status == "active")
    if kind is not None:
        query = query.where(DecisionRecordRow.kind == kind)
    if status is not None:
        query = query.where(DecisionRecordRow.status == status)
    if decided_by is not None:
        query = query.where(DecisionRecordRow.decided_by == decided_by)
    if decided_after is not None:
        query = query.where(DecisionRecordRow.decided_at >= decided_after)
    if decided_before is not None:
        query = query.where(DecisionRecordRow.decided_at < decided_before)
    query = query.order_by(DecisionRecordRow.decided_at.desc(),
                           DecisionRecordRow.created_at.desc(), DecisionRecordRow.id.desc())
    return list(await db.scalars(query.limit(max(1, min(limit, 200)))))


async def verify_fingerprint(db: AsyncSession, *, decision_id: str) -> bool:
    """True when the stored fingerprint matches the record as decided.

    The fingerprint covers the record in its active, as-decided form; a
    later supersession changes the status columns but not the stored
    fingerprint, so verification rebuilds the as-decided form first.
    """
    row_id, _ = _normalize_decision_id(decision_id)
    row = await db.get(DecisionRecordRow, row_id)
    if row is None:
        raise NotFoundError("Not found.")
    as_decided = replace(_row_to_pure(row), status="active", superseded_by=None, superseded_at=None)
    return decision_fingerprint(as_decided) == row.fingerprint
