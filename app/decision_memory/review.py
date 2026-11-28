"""Step M (slice 5): review lifecycle for decision records.

A decision is reviewed when its ``review_at`` date arrives (default:
decided date + 180 days). Reviewing is an explicit human act — nothing
here runs on a timer and nothing is automatic:

- **confirm** — the decision still stands. It stays active, its
  ``review_at`` moves to a new date (default: today + 180 days), and a
  ``reviewed`` event plus a ``decisions.reviewed`` audit row are written.
- **expire** — the decision has run its course (its context lapsed, it
  no longer applies). Status becomes ``expired``; it stays readable.
  For a sensitive kind (finance / supplier / people) an owner approval
  bound to the exact ``decision.expire`` action must be consumed first,
  exactly as revocation requires one.
- **supersede** — delegate to ``supersede_decision`` (slice 2): the old
  decision is superseded by a new one, atomically, with all of its
  approval rules intact. The review itself adds no extra event — the
  supersession is already recorded as ``superseded`` on the old row.
- **revoke** — delegate to ``revoke_decision`` (slice 2) with the
  reviewer's reason, with all of its approval rules intact.

Only an active decision can be reviewed. Every review needs
``decision.record`` on the scope (checked first, so a caller without
the permission gets 403, never a 404 about the decision). Separation of
duties is preserved by delegation: the person who records a supersession
cannot be its sole approver — the platform approval must be decided by
someone else, enforced by ``decide_approval``.

Surfacing: ``list_due_for_review`` returns active decisions whose
review date falls within a window (overdue first); ``review_digest`` is
the summary a scheduled report (Step AA) can consume — counts plus the
due rows. Both need ``decision.read`` and never reveal decisions from
another scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.decision_models import DecisionEventRow, DecisionRecordRow
from app.decision_memory.records import EvidenceRef
from app.decision_memory.service import (
    AUDIT_REVIEWED,
    DEFAULT_REVIEW_DAYS,
    PERMISSION_READ,
    PERMISSION_RECORD,
    SENSITIVE_KINDS,
    decision_action_spec,
    revoke_decision,
    supersede_decision,
)
from app.platform import access as platform_access
from app.platform.access import Principal
from app.platform.approvals import ActionSpec, ApprovalGrant, consume_approval
from app.platform.events import audit as platform_audit

__all__ = [
    "REVIEW_OUTCOMES",
    "REVIEW_SOON_DAYS",
    "ReviewDigest",
    "list_due_for_review",
    "review_decision",
    "review_digest",
]

REVIEW_OUTCOMES = ("confirm", "expire", "supersede", "revoke")

# A decision whose review date falls within this many days is "due soon"
# (the digest's second bucket). Overdue is anything before today.
REVIEW_SOON_DAYS = 7

MAX_REVIEW_REASON_CHARS = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_reason(reason: str | None) -> str:
    clean = " ".join(str(reason or "").split())
    if not 1 <= len(clean) <= MAX_REVIEW_REASON_CHARS:
        raise InvalidInputError(
            f"A review reason of 1–{MAX_REVIEW_REASON_CHARS} characters is required.")
    return clean


def _as_of_date(as_of: date | datetime | None) -> date:
    if as_of is None:
        return _now().date()
    return as_of.date() if isinstance(as_of, datetime) else as_of


async def _load_scoped_row(db: AsyncSession, scope_id: uuid.UUID, decision_id: str) -> DecisionRecordRow:
    """Load a decision row, or 404 — including when it exists in another scope."""
    try:
        row_id = uuid.UUID(decision_id)
    except (ValueError, AttributeError, TypeError):
        raise InvalidInputError("decision_id must be a UUID.") from None
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
    platform_audit(db, actor, action, target=target, details=details or {})


async def _consume_sensitive_approval(db: AsyncSession, *, spec: ActionSpec,
                                      approval_id: uuid.UUID | None) -> ApprovalGrant | None:
    """Consume the approval a sensitive decision needs, or refuse."""
    if approval_id is None:
        raise PermissionDeniedError(
            "Sensitive decisions need an owner approval first. Request one for this exact action, "
            "have someone else approve it, then retry with the approval id.")
    return await consume_approval(db, approval_id, spec)


async def review_decision(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                          decision_id: str,
                          outcome: Literal["confirm", "expire", "supersede", "revoke"],
                          reason: str,
                          review_at: date | datetime | None = None,
                          # Supersede outcome only: the new decision's fields.
                          title: str | None = None, rationale: str | None = None,
                          kind: str = "other",
                          decided_by: uuid.UUID | None = None,
                          decided_at: datetime | None = None,
                          alternatives_considered: tuple[str, ...] = (),
                          evidence: tuple[EvidenceRef, ...] = (),
                          depends_on: tuple[str, ...] = (),
                          decision_id_new: str | None = None,
                          approval_id: uuid.UUID | None = None,
                          ) -> DecisionRecordRow:
    """Review an active decision. Returns the row the review produced.

    - ``confirm`` / ``expire`` return the reviewed row itself;
    - ``supersede`` returns the new superseding row;
    - ``revoke`` returns the revoked row.

    ``reason`` (1–500 characters) is required for every outcome: it is
    the reviewer's note on why this outcome was chosen, and it is stored
    on the lifecycle event. A supersede also needs the new decision's
    title and rationale; a revoke needs nothing else.
    """
    if outcome not in REVIEW_OUTCOMES:
        raise InvalidInputError(f"Unknown review outcome {outcome!r}.")
    clean_reason = _clean_reason(reason)
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    row = await _load_scoped_row(db, scope_id, decision_id)
    if row.status != "active":
        raise InvalidInputError(
            f"Only an active decision can be reviewed, got {row.status!r}.")

    if outcome == "supersede":
        if title is None or rationale is None:
            raise InvalidInputError(
                "A superseding review needs the new decision's title and rationale.")
        new_row, _old_row = await supersede_decision(
            db, actor, scope_id=scope_id, superseded_id=str(row.id),
            title=title, rationale=rationale, kind=kind,
            decided_by=decided_by, decided_at=decided_at,
            alternatives_considered=tuple(alternatives_considered),
            evidence=tuple(evidence),
            depends_on=tuple(str(item) for item in depends_on),
            review_at=review_at,
            decision_id=decision_id_new,
            approval_id=approval_id,
        )
        return new_row

    if outcome == "revoke":
        return await revoke_decision(
            db, actor, scope_id=scope_id, decision_id=str(row.id),
            reason=clean_reason, approval_id=approval_id)

    # Confirm and expire are decided here; both write a "reviewed"
    # lifecycle event and one audit row.
    today = _now().date()
    if outcome == "confirm":
        if review_at is None:
            next_review = today + timedelta(days=DEFAULT_REVIEW_DAYS)
        else:
            next_review = review_at.date() if isinstance(review_at, datetime) else review_at
            if next_review < today:
                raise InvalidInputError("The next review date must not be in the past.")
            if next_review < row.decided_at.date():
                raise InvalidInputError("The next review date must not be before the decision date.")
        row.review_at = next_review
        row.updated_at = _now()
        await db.flush()
        _append_event(db, decision_id=row.id, event="reviewed", actor=actor,
                      reason=clean_reason,
                      details={"outcome": "confirm", "next_review_at": next_review.isoformat()})
        _audit(db, actor, AUDIT_REVIEWED, target=str(row.id),
               details={"outcome": "confirm", "reason": clean_reason,
                        "next_review_at": next_review.isoformat()})
        return row

    # outcome == "expire"
    if row.kind in SENSITIVE_KINDS:
        spec = decision_action_spec(action_type="decision.expire", scope_id=scope_id,
                                    decision_id=str(row.id), kind=row.kind)
        await _consume_sensitive_approval(db, spec=spec, approval_id=approval_id)
    row.status = "expired"
    row.updated_at = _now()
    await db.flush()
    _append_event(db, decision_id=row.id, event="reviewed", actor=actor,
                  reason=clean_reason, details={"outcome": "expire", "kind": row.kind})
    _audit(db, actor, AUDIT_REVIEWED, target=str(row.id),
           details={"outcome": "expire", "kind": row.kind, "reason": clean_reason})
    return row


async def list_due_for_review(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                              as_of: date | datetime | None = None,
                              days_ahead: int = REVIEW_SOON_DAYS,
                              limit: int = 100) -> list[DecisionRecordRow]:
    """Active decisions whose review date falls within the window.

    Needs ``decision.read``. Most overdue first (``review_at`` ascending),
    then newest decided. ``as_of`` defaults to today; ``days_ahead``
    (0–365) extends the window past today. Decisions without a review
    date are never due.
    """
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    if not 0 <= days_ahead <= 365:
        raise InvalidInputError("days_ahead must be 0–365.")
    anchor = _as_of_date(as_of)
    cutoff = anchor + timedelta(days=days_ahead)
    query = (select(DecisionRecordRow)
             .where(DecisionRecordRow.scope_id == scope_id,
                    DecisionRecordRow.status == "active",
                    DecisionRecordRow.review_at.is_not(None),
                    DecisionRecordRow.review_at <= cutoff)
             .order_by(DecisionRecordRow.review_at.asc(),
                       DecisionRecordRow.decided_at.desc(),
                       DecisionRecordRow.created_at.desc(),
                       DecisionRecordRow.id.desc())
             .limit(max(1, min(limit, 200))))
    return list(await db.scalars(query))


@dataclass(frozen=True)
class ReviewDigest:
    """The review summary for one scope — counts plus the due rows.

    Built for a scheduled-report consumer (Step AA): ``overdue_count``
    is decisions whose review date has passed, ``due_soon_count`` is
    decisions due within the window, ``needs_review_count`` is active
    decisions whose rationale was flagged as instruction-like. ``due``
    is the same list ``list_due_for_review`` returns, most overdue first.
    """

    scope_id: uuid.UUID
    as_of: date
    days_ahead: int
    overdue_count: int
    due_soon_count: int
    total_active: int
    needs_review_count: int
    due: tuple[DecisionRecordRow, ...]


async def review_digest(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                        as_of: date | datetime | None = None,
                        days_ahead: int = REVIEW_SOON_DAYS,
                        limit: int = 100) -> ReviewDigest:
    """Counts + due rows for the scope's review queue. Needs ``decision.read``."""
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    if not 0 <= days_ahead <= 365:
        raise InvalidInputError("days_ahead must be 0–365.")
    anchor = _as_of_date(as_of)
    cutoff = anchor + timedelta(days=days_ahead)
    base = [DecisionRecordRow.scope_id == scope_id, DecisionRecordRow.status == "active"]
    overdue_count = await db.scalar(select(func.count()).select_from(DecisionRecordRow).where(
        *base, DecisionRecordRow.review_at.is_not(None),
        DecisionRecordRow.review_at < anchor)) or 0
    due_soon_count = await db.scalar(select(func.count()).select_from(DecisionRecordRow).where(
        *base, DecisionRecordRow.review_at.is_not(None),
        DecisionRecordRow.review_at >= anchor, DecisionRecordRow.review_at <= cutoff)) or 0
    total_active = await db.scalar(select(func.count()).select_from(DecisionRecordRow).where(
        *base)) or 0
    needs_review_count = await db.scalar(select(func.count()).select_from(DecisionRecordRow).where(
        *base, DecisionRecordRow.needs_review.is_(True))) or 0
    due = await list_due_for_review(db, actor, scope_id=scope_id, as_of=anchor,
                                    days_ahead=days_ahead, limit=limit)
    return ReviewDigest(
        scope_id=scope_id, as_of=anchor, days_ahead=days_ahead,
        overdue_count=int(overdue_count), due_soon_count=int(due_soon_count),
        total_active=int(total_active), needs_review_count=int(needs_review_count),
        due=tuple(due),
    )
