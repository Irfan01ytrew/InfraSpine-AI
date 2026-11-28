"""Twin-driven proposals service (Step K v2 slice 6).

Async service over the stored proposals. A proposal is created
from one twin finding (a drill-reconciliation queue row, or a
simulation finding), requests a platform approval bound to the
exact action, and — only once approved — is dispatched through
``execute_once`` with read-back verification.

The lifecycle: ``draft`` → ``pending_approval`` → ``approved`` |
``rejected`` | ``expired`` | ``cancelled`` → ``dispatched`` →
``verified`` | ``failed``. The platform approval row is the
source of truth for approve/reject/expire; this row mirrors it.
A rejected / expired / failed proposal may be re-proposed from
the same finding (a fresh approval is requested; the old
approval rows stay in the approval table as history). A
cancelled proposal is a deliberate human stop and stays
cancelled — propose a different action kind for that finding.

Separation of duties is three-way and enforced here, not only
by the platform: the executor may be neither the proposer nor
the approver. (``decide_approval`` already blocks the proposer
from approving; ``execute_once`` checks the duty permission and
the approval binding.)

What dispatch DOES: the approved action is published as a
durable ``twin.proposal_action`` outbox event carrying the exact
canonical action. The read-back verification proves the event
row exists with the exact payload (action hash + proposal id);
the platform dispatcher then delivers it at-least-once. The
twin never writes the graph, grants access, restores data,
switches a supplier, or pays: the physical follow-up (assigning
a backup, reviewing a supplier) is a human/operator step the
event triggers. Money never enters the action params; the
finding's money difference lives on the proposal row's finding
summary and is redacted for non-owners at read time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.twin_proposal_models import (
    PROPOSAL_STATUSES,
    TERMINAL_PROPOSAL_STATUSES,
    TwinProposal,
)
from app.digital_twin import proposals
from app.platform.access import Principal
from app.platform.approvals import ActionSpec, decide_approval, request_approval
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformApprovalRequest, PlatformOutbox
from app.platform.side_effects import module_scope_id

#: Approval lifetime for a twin proposal (within the platform's
#: 1-minute to 7-day bound). A day is long enough for a human to
#: review; the approval still expires and is single-use.
DEFAULT_PROPOSAL_TTL = timedelta(hours=24)

#: Proposal retention defaults (same 90-day age rule as the
#: reconciliation findings; 100 proposals is the per-tenant cap).
#: Only terminal proposals are ever pruned — a pending or
#: approved proposal must never vanish from the inbox.
DEFAULT_PROPOSAL_RETENTION_COUNT = 100
DEFAULT_PROPOSAL_RETENTION_DAYS = 90

#: Hard cap on list responses so the proposal inbox stays bounded.
MAX_LIST_LIMIT = 100

#: Statuses a stored proposal may be re-proposed from (a fresh
#: approval is requested; the old approval rows stay as history).
REOPENABLE_STATUSES = ("rejected", "expired", "failed")

#: Statuses an execute call may run or replay from.
EXECUTABLE_STATUSES = ("approved", "dispatched", "verified")

__all__ = [
    "DEFAULT_PROPOSAL_RETENTION_COUNT",
    "DEFAULT_PROPOSAL_RETENTION_DAYS",
    "DEFAULT_PROPOSAL_TTL",
    "EXECUTABLE_STATUSES",
    "MAX_LIST_LIMIT",
    "REOPENABLE_STATUSES",
    "cancel_proposal",
    "create_proposal",
    "decide_proposal",
    "execute_proposal",
    "get_proposal",
    "list_proposals",
    "proposal_counts",
    "proposal_summary",
    "prune_proposals",
    "redact_money_for_viewer",
]


def proposal_summary(row):
    """Small read-only summary of a stored proposal row."""
    approver_class = {permission: klass for klass, permission
                      in proposals.REQUIRED_PERMISSION.items()}.get(row.required_permission)
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "source": row.source,
        "finding_fingerprint": row.finding_fingerprint,
        "finding": row.finding_summary,
        "money_redacted": False,
        "proposal_fingerprint": row.proposal_fingerprint,
        "action_type": row.action_type,
        "action": row.action,
        "action_hash": row.action_hash,
        "approver_class": approver_class,
        "required_permission": row.required_permission,
        "duty": row.duty,
        "status": row.status,
        "approval_id": str(row.approval_id) if row.approval_id else None,
        "execution_id": str(row.execution_id) if row.execution_id else None,
        "external_ref": row.external_ref,
        "proposed_by": str(row.proposed_by),
        "decided_by": str(row.decided_by) if row.decided_by else None,
        "created_at": row.created_at.isoformat(),
    }


def redact_money_for_viewer(summary):
    """Copy of a proposal summary with the finding's money hidden.

    ``None`` in → ``None`` out. The owner-only rule (plan §4):
    exposure amounts stay owner-only; the finding code, severity,
    and the action itself are safe for any tenant member.
    """
    if summary is None:
        return None
    copy = dict(summary)
    finding = copy.get("finding")
    if isinstance(finding, dict):
        finding = dict(finding)
        finding["money"] = None
        copy["finding"] = finding
    copy["money_redacted"] = True
    return copy


def _check_tenant(tenant_id):
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    return tenant_id


def _spec_from_canonical(action):
    """Rebuild the ActionSpec a draft's canonical dict describes.

    Fail-closed: the rebuilt spec's hash must equal the stored
    action hash — a tampered stored action never dispatches.
    """
    if not isinstance(action, dict):
        raise ValueError("proposal action must be a canonical ActionSpec dict.")
    try:
        spec = ActionSpec(
            action["type"],
            UUID(str(action["scope_id"])),
            dict(action["params"]),
            amount=None,
            currency=None,
            subject=action["subject"],
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"proposal action is malformed: {exc}.") from exc
    return spec


async def _load(db, tenant_id, proposal_id):
    if not isinstance(proposal_id, UUID):
        raise ValueError(f"proposal_id must be a UUID, got {proposal_id!r}.")
    return await db.scalar(
        select(TwinProposal).where(
            TwinProposal.tenant_id == tenant_id,
            TwinProposal.id == proposal_id,
        )
    )


async def create_proposal(db, principal, tenant_id, *, finding, action_kind):
    """Propose an action for one twin finding.

    Idempotent per (tenant, finding, action): re-proposing the
    same finding for the same action returns the existing
    proposal while it is actionable. A rejected / expired /
    failed proposal may be re-proposed (a fresh approval is
    requested). A cancelled proposal stays cancelled.
    Returns ``{"proposal", "created", "approval_id"}``.
    """
    _check_tenant(tenant_id)
    if not isinstance(principal, Principal):
        raise ValueError("principal must be a platform Principal.")
    scope_id = await module_scope_id(db, "company_graph", tenant_id)
    if scope_id is None:
        raise NotFoundError("Company graph not found.")
    draft = proposals.draft_proposal(
        tenant_id, scope_id, finding, action_kind, proposed_by=principal.user_id)
    existing = await db.scalar(
        select(TwinProposal).where(
            TwinProposal.tenant_id == tenant_id,
            TwinProposal.proposal_fingerprint == draft["proposal_fingerprint"],
        )
    )
    if existing is not None:
        if existing.status == "cancelled":
            raise ConflictError(
                "This proposal was cancelled. Propose a different action for this finding.")
        if existing.status not in REOPENABLE_STATUSES:
            return {"proposal": proposal_summary(existing), "created": False,
                    "approval_id": str(existing.approval_id) if existing.approval_id else None}
        row = existing
        row.status = "draft"
        row.approval_id = None
        row.execution_id = None
        row.external_ref = None
        row.decided_by = None
        await db.flush()
    else:
        row = TwinProposal(
            tenant_id=tenant_id,
            source=draft["finding"]["source"],
            finding_fingerprint=draft["finding"]["fingerprint"],
            finding_summary={
                "code": draft["finding"]["code"],
                "resource_id": draft["finding"]["resource_id"],
                "severity": draft["finding"]["severity"],
                "money": draft["finding"]["money"],
                "detail": draft["finding"]["detail"],
            },
            proposal_fingerprint=draft["proposal_fingerprint"],
            action_type=draft["action_kind"],
            action=draft["action"],
            action_hash=draft["action_hash"],
            required_permission=draft["required_permission"],
            duty=draft["duty"],
            status="draft",
            proposed_by=principal.user_id,
        )
        db.add(row)
        await db.flush()
    spec = _spec_from_canonical(draft["action"])
    if spec.hash() != draft["action_hash"]:
        raise ValueError("draft action hash does not match its canonical action.")
    approval = await request_approval(
        db, principal, spec,
        required_permission=draft["required_permission"],
        ttl=DEFAULT_PROPOSAL_TTL,
    )
    # request_approval commits (the draft row commits with it).
    row.approval_id = approval.id
    row.status = "pending_approval"
    await db.flush()
    await db.commit()
    await db.refresh(row)
    return {"proposal": proposal_summary(row), "created": True,
            "approval_id": str(approval.id)}


async def get_proposal(db, tenant_id, proposal_id):
    """One stored proposal summary, or ``None``."""
    _check_tenant(tenant_id)
    row = await _load(db, tenant_id, proposal_id)
    return proposal_summary(row) if row is not None else None


async def list_proposals(db, tenant_id, *, limit=30, status=None, action_type=None):
    """Stored proposals, newest first, with optional filters."""
    _check_tenant(tenant_id)
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    query = select(TwinProposal).where(TwinProposal.tenant_id == tenant_id)
    if status is not None:
        if status not in PROPOSAL_STATUSES:
            raise ValueError(f"unknown proposal status: {status!r}.")
        query = query.where(TwinProposal.status == status)
    if action_type is not None:
        if action_type not in proposals.PROPOSAL_ACTIONS:
            raise ValueError(f"unknown proposal action kind: {action_type!r}.")
        query = query.where(TwinProposal.action_type == action_type)
    rows = (
        await db.scalars(
            query.order_by(TwinProposal.created_at.desc(), TwinProposal.id.desc())
            .limit(clamped)
        )
    ).all()
    return [proposal_summary(row) for row in rows]


async def proposal_counts(db, tenant_id):
    """Proposal inbox counts for one tenant."""
    _check_tenant(tenant_id)
    rows = (
        await db.scalars(
            select(TwinProposal).where(TwinProposal.tenant_id == tenant_id)
        )
    ).all()
    counts = {"total": len(rows)}
    for status in PROPOSAL_STATUSES:
        counts[status] = 0
    for row in rows:
        counts[row.status] += 1
    counts["actionable"] = counts["pending_approval"] + counts["approved"]
    return counts


async def decide_proposal(db, principal, tenant_id, proposal_id, *, decision,
                          presented_hash, note=""):
    """Decide a pending proposal (approve / reject).

    The platform approval decides the action; this mirrors the
    outcome onto the proposal row. Returns the summary, or
    ``None`` when the proposal does not belong to this tenant.
    An expired approval marks the proposal expired too.
    """
    _check_tenant(tenant_id)
    if not isinstance(principal, Principal):
        raise ValueError("principal must be a platform Principal.")
    row = await _load(db, tenant_id, proposal_id)
    if row is None:
        return None
    if row.status != "pending_approval":
        raise ConflictError(f"This proposal is {row.status}, not pending approval.")
    if row.approval_id is None:
        raise ConflictError("This proposal has no approval request.")
    try:
        decided = await decide_approval(
            db, principal, row.approval_id, decision,
            presented_hash=presented_hash, note=note)
    except ConflictError:
        # decide_approval marks an expired approval expired and
        # commits before raising — mirror that onto the proposal.
        approval = await db.get(PlatformApprovalRequest, row.approval_id)
        if approval is not None and approval.status == "expired":
            row.status = "expired"
            await db.flush()
            await db.commit()
        raise
    row.status = decided.status
    row.decided_by = decided.decided_by
    await db.flush()
    await db.commit()
    await db.refresh(row)
    return proposal_summary(row)


async def _dispatch_event(db, principal, row, scope_id):
    """Publish the approved action as a durable outbox event.

    Returns the outbox row id (the execute_once external ref).
    """
    event = PlatformOutbox(
        event_type="twin.proposal_action",
        scope_id=scope_id,
        payload={
            "proposal_id": str(row.id),
            "tenant_id": str(row.tenant_id),
            "action_kind": row.action_type,
            "action": row.action,
            "action_hash": row.action_hash,
            "approval_id": str(row.approval_id),
        },
        trace_id=principal.trace_id,
    )
    db.add(event)
    await db.flush()
    return str(event.id)


async def _verify_event(db, principal, row, action_hash):
    """Read back the dispatched event: it must exist with the exact payload."""
    async def verify(ref):
        try:
            event_id = int(ref)
        except (TypeError, ValueError):
            return VerifiedResult(False, {"external_ref": ref, "reason": "bad_ref"})
        event = await db.get(PlatformOutbox, event_id)
        if event is None:
            return VerifiedResult(False, {"external_ref": ref, "reason": "event_missing"})
        payload = event.payload or {}
        ok = (event.event_type == "twin.proposal_action"
              and payload.get("action_hash") == action_hash
              and payload.get("proposal_id") == str(row.id))
        return VerifiedResult(ok, {
            "external_ref": ref,
            "event_type": event.event_type,
            "dispatched": event.dispatched_at is not None,
        })

    return verify


async def execute_proposal(db, principal, tenant_id, proposal_id):
    """Dispatch an approved proposal through ``execute_once``.

    The executor may be neither the proposer nor the approver
    (three-way separation of duties). The idempotency key is
    bound to the approval, so each approval attempt dispatches
    at most once and a re-execute replays the stored result.
    Returns ``{"proposal", "execution", "replayed"}``.
    """
    _check_tenant(tenant_id)
    if not isinstance(principal, Principal):
        raise ValueError("principal must be a platform Principal.")
    row = await _load(db, tenant_id, proposal_id)
    if row is None:
        return None
    if row.status not in EXECUTABLE_STATUSES:
        raise ConflictError(f"This proposal is {row.status} and cannot be executed.")
    if row.approval_id is None:
        raise ConflictError("This proposal has no approval.")
    if principal.user_id == row.proposed_by:
        raise PermissionDeniedError(
            "Separation of duties: the proposer cannot execute their own proposal.")
    if row.decided_by is not None and principal.user_id == row.decided_by:
        raise PermissionDeniedError(
            "Separation of duties: the approver cannot execute the proposal they approved.")
    approval = await db.get(PlatformApprovalRequest, row.approval_id)
    if approval is None:
        raise ConflictError("This proposal has no approval.")
    if approval.status == "expired":
        row.status = "expired"
        await db.flush()
        await db.commit()
        raise ConflictError("This approval expired. Request a new one.")
    if approval.status not in ("approved", "consumed"):
        raise ConflictError("This proposal is not approved yet.")
    spec = _spec_from_canonical(row.action)
    if spec.hash() != row.action_hash:
        raise ValueError("Stored proposal action does not match its action hash.")
    scope_id = UUID(str(row.action["scope_id"]))
    verify = await _verify_event(db, principal, row, row.action_hash)

    async def dispatch():
        return await _dispatch_event(db, principal, row, scope_id)

    result = await execute_once(
        db, principal, f"twin-proposal:{row.approval_id}", spec,
        duty=row.duty, dispatch=dispatch, verify=verify,
        approval_id=row.approval_id,
    )
    row.execution_id = result.execution_id
    row.external_ref = result.external_ref
    if result.status == "verified":
        row.status = "verified"
    elif result.status == "dispatched":
        row.status = "dispatched"
    elif result.status == "failed":
        row.status = "failed"
    elif result.status == "claimed":
        row.status = "dispatched"
    await db.flush()
    await db.commit()
    await db.refresh(row)
    return {
        "proposal": proposal_summary(row),
        "execution": {
            "execution_id": str(result.execution_id),
            "status": result.status,
            "external_ref": result.external_ref,
            "verified_result": result.verified_result,
        },
        "replayed": result.replayed,
    }


async def cancel_proposal(db, principal, tenant_id, proposal_id):
    """Cancel a draft / pending proposal (proposer only).

    The pending approval is cancelled with it. A cancelled
    proposal stays cancelled — propose a different action for
    the finding. Returns the summary, or ``None``.
    """
    _check_tenant(tenant_id)
    if not isinstance(principal, Principal):
        raise ValueError("principal must be a platform Principal.")
    row = await _load(db, tenant_id, proposal_id)
    if row is None:
        return None
    if principal.user_id != row.proposed_by:
        raise PermissionDeniedError("Only the proposer can cancel this proposal.")
    if row.status not in ("draft", "pending_approval"):
        raise ConflictError(f"This proposal is {row.status} and cannot be cancelled.")
    if row.approval_id is not None:
        approval = await db.get(PlatformApprovalRequest, row.approval_id)
        if approval is not None and approval.status == "pending":
            approval.status = "cancelled"
    row.status = "cancelled"
    await db.flush()
    await db.commit()
    await db.refresh(row)
    return proposal_summary(row)


async def prune_proposals(db, tenant_id, *, keep=DEFAULT_PROPOSAL_RETENTION_COUNT,
                          max_age_days=DEFAULT_PROPOSAL_RETENTION_DAYS, now=None):
    """Delete terminal proposals beyond the retention rule; returns how many.

    A proposal leaves only when it is terminal (rejected /
    expired / cancelled / verified / failed) AND older than
    ``max_age_days`` OR outside the newest ``keep`` terminal
    proposals. Pending, approved, and dispatched proposals are
    never pruned — an actionable proposal must not vanish.
    """
    _check_tenant(tenant_id)
    if keep < 1:
        raise ValueError("keep must be at least 1.")
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative.")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=max_age_days)
    rows = (
        await db.scalars(
            select(TwinProposal)
            .where(TwinProposal.tenant_id == tenant_id,
                   TwinProposal.status.in_(TERMINAL_PROPOSAL_STATUSES))
            .order_by(TwinProposal.created_at.desc(), TwinProposal.id.desc())
        )
    ).all()
    doomed = [
        row.id
        for index, row in enumerate(rows)
        if index >= keep or row.created_at < cutoff
    ]
    if doomed:
        await db.execute(
            delete(TwinProposal).where(TwinProposal.id.in_(doomed))
        )
    return len(doomed)
