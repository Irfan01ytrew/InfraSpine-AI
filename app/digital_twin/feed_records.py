"""Feed changes into the governed-change path (Step K v2 slice 7).

Async (sqlalchemy). ``apply_feed`` turns one translated feed change
(``app.digital_twin.feed_changes.translate_event``) into a pending
``GraphChange`` — a proposal a human approves or rejects. The twin
NEVER writes the graph directly: no record is created, updated or
retired here, no membership is granted or revoked here.

Rules (mirroring ``app.company_graph.service``):

- Tenant resolution: the event payload's ``tenant_id`` first (when
  publishers include the graph tenant), else the outbox
  ``scope_id`` when it IS a graph tenant id. A graph tenant id is
  NOT an org id — the two are unrelated UUIDs — so an event that
  carries neither is skipped with a warning, not guessed at.
- Requester: the event actor when one is given and is an active
  user, else the tenant's first active owner member (by user id —
  deterministic, not a preference). The twin has no user of its
  own; the owner is the human accountable for the twin's proposals.
  A tenant with no requester cannot be proposed for — that is a
  ``ValueError`` (the outbox retries, then dead-letters it for an
  operator), never a silent drop.
- Records: external key ``twin-feed:{feed}:{subject_ref}``; kind is
  the feed subject kind; visibility ``restricted``; one evidence
  entry naming the source event; ``inferred=True`` (machine text);
  ``retired=True`` for removals. ``base_version`` resolves from the
  live record (0 = new), ``subject_id`` is the live record id when
  one exists, else a deterministic uuid5 so a retry proposes the
  same subject. A removal with no live record is skipped — there is
  nothing to retire.
- Members: role ``viewer`` for a new member (never a privilege
  grant), the existing role/departments preserved for an update,
  ``active`` from the action (upsert = active, remove = inactive).
  A removal with no live member row is skipped.
- Dedupe (``discovery.py`` pattern): an identical pending change —
  same subject, same payload ignoring timestamps (each proposal
  stamps ``now``), same base_version — is skipped, so at-least-once
  delivery never spams the review queue.
  A superseded base_version (the graph moved on) is a NEW proposal:
  ``decide()`` will refuse it with a version conflict, which is the
  governed path working as designed.
- Expiry: members +1h, records +24h (same as ``propose_*``).
- Every proposal is audited as ``twin.feed_proposed``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.company_graph.models import GraphChange, GraphMember, GraphRecord, GraphTenant
from app.company_graph.schemas import MemberInput, RecordInput
from app.db.models import User
from app.digital_twin import feed_changes
from app.services.audit import AuditContext, record as audit_record

logger = logging.getLogger("app.digital_twin.feed_records")

_MEMBER_EXPIRY = timedelta(hours=1)
_RECORD_EXPIRY = timedelta(hours=24)


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _payload_identity(payload: dict) -> str:
    """Canonical identity of a change payload, ignoring timestamps.

    Two proposals of the same change carry different ``valid_from`` /
    ``observed_at`` stamps (each call stamps ``now``); the change
    itself is the same. Comparing the full payload would make dedupe
    never fire, so timestamps are excluded from the identity.
    """
    identity = {key: value for key, value in payload.items()
                if key not in ("valid_from", "observed_at")}
    evidence = identity.get("evidence")
    if isinstance(evidence, list):
        identity["evidence"] = [
            {key: value for key, value in entry.items() if key != "observed_at"}
            if isinstance(entry, dict) else entry for entry in evidence]
    return _canonical(identity)


def _parse_uuid(value, *, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{what} must be a UUID, got {value!r}") from None


async def resolve_tenant(db: AsyncSession, event) -> uuid.UUID | None:
    """Graph tenant for an outbox event, or None when unresolvable.

    Payload ``tenant_id`` first, then the outbox ``scope_id`` — but
    only when the id is actually a graph tenant. Org ids and other
    scope ids never resolve here (a graph tenant id is not an org
    id); those events are skipped by the caller with a warning.
    """
    candidates = []
    payload = event.payload or {}
    if isinstance(payload, dict) and payload.get("tenant_id"):
        candidates.append(payload.get("tenant_id"))
    if event.scope_id is not None:
        candidates.append(event.scope_id)
    for candidate in candidates:
        try:
            tenant_id = _parse_uuid(candidate, what="tenant id")
        except ValueError:
            continue
        if await db.scalar(select(GraphTenant.id).where(GraphTenant.id == tenant_id)):
            return tenant_id
    return None


async def resolve_requester(db: AsyncSession, tenant_id: uuid.UUID,
                            actor_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Who owns a feed proposal: the event actor, else first owner.

    Returns None when the tenant has no active owner member — the
    caller treats that as an unprocessable event.
    """
    if actor_id is not None:
        user = await db.scalar(select(User).where(User.id == actor_id, User.is_active.is_(True)))
        if user is not None:
            return user.id
    owner = await db.scalar(select(GraphMember).join(User, User.id == GraphMember.user_id).where(
        GraphMember.tenant_id == tenant_id, GraphMember.role == "owner",
        GraphMember.active.is_(True), User.is_active.is_(True),
    ).order_by(GraphMember.user_id))
    return owner.user_id if owner is not None else None


async def apply_feed(db: AsyncSession, tenant_id: uuid.UUID, change: dict, *,
                     requested_by: uuid.UUID | None = None,
                     observed_at: datetime | None = None) -> dict:
    """Store one feed change as a pending GraphChange proposal.

    Returns ``{"change_id", "change_type", "subject_id",
    "base_version", "skipped": None}`` on success, or
    ``{"skipped": "<reason>"}`` when there is nothing to propose
    (duplicate pending change, removal of a subject that does not
    exist). Raises ``ValueError`` for a malformed change or a tenant
    with no requester.
    """
    if not isinstance(change, dict):
        raise ValueError(f"feed change must be a dict, got {type(change).__name__}")
    feed = change.get("feed")
    subject_kind = change.get("subject_kind")
    subject_ref = change.get("subject_ref")
    action = change.get("action")
    if feed not in ("membership", "workflow", "agent_studio", "connectors"):
        raise ValueError(f"unknown feed {feed!r}")
    if not isinstance(subject_kind, str) or not subject_kind:
        raise ValueError(f"feed change needs a subject_kind, got {change!r}")
    if not isinstance(subject_ref, str) or not subject_ref:
        raise ValueError(f"feed change needs a subject_ref, got {change!r}")
    if action not in ("upsert", "remove"):
        raise ValueError(f"feed change action must be upsert/remove, got {action!r}")
    name = change.get("name")
    if not isinstance(name, str) or not name:
        name = subject_ref
    now = datetime.now(UTC)
    observed = observed_at or now
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)

    requester_id = await resolve_requester(db, tenant_id, requested_by)
    if requester_id is None:
        raise ValueError(f"no requester available for tenant {tenant_id} (no active owner member)")
    requester = await db.scalar(select(User).where(User.id == requester_id))
    email = requester.email if requester is not None else None

    if subject_kind == "member":
        subject_user_id = _parse_uuid(subject_ref, what="member subject_ref")
        member = await db.scalar(select(GraphMember).where(
            GraphMember.tenant_id == tenant_id, GraphMember.user_id == subject_user_id))
        if action == "remove" and member is None:
            return {"skipped": "not_a_member"}
        # The twin never grants privilege: a new member is proposed
        # as "viewer"; an existing member keeps its role. The event
        # payload's role (if any) is deliberately ignored.
        role = member.role if member is not None else "viewer"
        payload = MemberInput(
            user_id=subject_user_id,
            role=role,
            departments=list(member.departments) if member is not None else [],
            all_departments=bool(member.all_departments) if member is not None else False,
            active=action == "upsert",
            base_version=member.version if member is not None else 0,
        )
        change_type = "member"
        subject_id = subject_user_id
        base_version = payload.base_version
        expires_at = now + _MEMBER_EXPIRY
    else:
        external_key = f"twin-feed:{feed}:{subject_ref}"
        record = await db.scalar(select(GraphRecord).where(
            GraphRecord.tenant_id == tenant_id, GraphRecord.external_key == external_key))
        if action == "remove" and record is None:
            return {"skipped": "not_present"}
        attributes = dict(change.get("attributes") or {})
        attributes.setdefault("feed_subject_ref", subject_ref)
        evidence = [{
            "source": f"twin-feed:{feed}",
            "reference": str(change.get("source_event") or feed),
            "quote": f"Twin feed {feed}: {change.get('source_event') or feed} for {subject_ref}"[:500],
            "observed_at": observed.isoformat(),
        }]
        payload = RecordInput(
            record_type="entity",
            external_key=external_key,
            kind=subject_kind,
            name=name[:240],
            visibility="restricted",
            readers=[],
            attributes=attributes,
            evidence=evidence,
            confidence=1.0,
            inferred=True,
            retired=action == "remove",
            valid_from=observed,
            valid_to=None,
            observed_at=observed,
        )
        change_type = "record"
        subject_id = record.id if record is not None else uuid.uuid5(
            uuid.NAMESPACE_URL, f"infraspine:twin-feed:{tenant_id}:{feed}:{subject_ref}")
        base_version = record.version if record is not None else 0
        expires_at = now + _RECORD_EXPIRY

    payload_json = payload.model_dump(mode="json")
    duplicate = await db.scalar(select(GraphChange).where(
        GraphChange.tenant_id == tenant_id, GraphChange.subject_id == subject_id,
        GraphChange.status == "pending",
    ).order_by(GraphChange.id.desc()))
    if duplicate is not None and _payload_identity(duplicate.payload) == _payload_identity(payload_json) \
            and duplicate.base_version == base_version:
        return {"skipped": "duplicate_pending", "change_id": str(duplicate.id)}
    row = GraphChange(
        tenant_id=tenant_id, change_type=change_type, subject_id=subject_id, payload=payload_json,
        base_version=base_version, requested_by=requester_id, expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    audit_record(db, AuditContext(requester_id, email, None, None), "twin.feed_proposed",
                 target=str(row.id), details={
                     "feed": feed, "subject_kind": subject_kind, "subject_ref": subject_ref,
                     "action": action, "fingerprint": feed_changes.feed_change_fingerprint(change),
                 })
    logger.info("twin feed proposed %s %s %s -> change %s", feed, subject_kind, subject_ref, row.id)
    return {"change_id": str(row.id), "change_type": change_type, "subject_id": str(subject_id),
            "base_version": base_version, "skipped": None}
