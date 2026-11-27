"""Approvals bound to the exact action: an approval covers one canonical ActionSpec (type, scope, parameters,
amount, currency, subject), expires, is used at most once, and is never decided by its requester or by someone who
performed another separated duty on the same subject."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.platform.access import SEPARATED_DUTIES, Principal, authorize, require
from app.platform.events import audit, publish
from app.platform.models import PlatformActionExecution, PlatformApprovalRequest

DEFAULT_APPROVAL_TTL = timedelta(hours=1)
MAX_APPROVAL_TTL = timedelta(days=7)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        raise InvalidInputError("Use Decimal or strings for amounts and fractional values in an action.")
    raise InvalidInputError(f"Unsupported value type in an action: {type(value).__name__}.")


@dataclass(frozen=True)
class ActionSpec:
    """The exact thing that will happen. Never put secrets in params: the canonical form is stored for review."""

    type: str
    scope_id: uuid.UUID
    params: dict = field(default_factory=dict)
    amount: Decimal | None = None
    currency: str | None = None
    subject: str | None = None

    def canonical(self) -> dict:
        return _canonical({"type": self.type, "scope_id": self.scope_id, "params": self.params,
                           "amount": self.amount, "currency": (self.currency or "").upper() or None,
                           "subject": self.subject})

    def hash(self) -> str:
        encoded = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: uuid.UUID
    action_hash: str
    approved_by: uuid.UUID
    required_permission: str


def _now() -> datetime:
    return datetime.now(UTC)


async def assert_separation_of_duties(db: AsyncSession, user_id: uuid.UUID, subject: str | None, duty: str) -> None:
    """One person performs at most one separated duty (verify / approve payment / transfer) per subject."""
    if not subject or duty not in SEPARATED_DUTIES:
        return
    approved = set(await db.scalars(select(PlatformApprovalRequest.required_permission).where(
        PlatformApprovalRequest.subject == subject, PlatformApprovalRequest.decided_by == user_id,
        PlatformApprovalRequest.status.in_(("approved", "consumed")),
    )))
    executed = set(await db.scalars(select(PlatformActionExecution.duty).where(
        PlatformActionExecution.subject == subject, PlatformActionExecution.executor_user_id == user_id,
        PlatformActionExecution.status != "failed",
    )))
    other = (approved | executed) & SEPARATED_DUTIES - {duty}
    if other:
        raise PermissionDeniedError(
            f"Separation of duties: you already performed {sorted(other)[0]} for {subject}; another person must do this."
        )


async def request_approval(db: AsyncSession, p: Principal, action: ActionSpec, *, required_permission: str,
                           ttl: timedelta = DEFAULT_APPROVAL_TTL) -> PlatformApprovalRequest:
    if not timedelta(minutes=1) <= ttl <= MAX_APPROVAL_TTL:
        raise InvalidInputError("Approval lifetime must be between 1 minute and 7 days.")
    decision = await authorize(db, p, "approval.request", action.scope_id)
    if not decision.allowed and decision.reason in {"scope_missing", "not_a_member"}:
        raise NotFoundError("Not found.")
    if not decision.allowed and decision.reason != "permission_missing":
        raise PermissionDeniedError("You cannot request approvals right now.")
    row = PlatformApprovalRequest(
        scope_id=action.scope_id, action_type=action.type, action_hash=action.hash(), action=action.canonical(),
        subject=action.subject, amount=action.amount, currency=(action.currency or "").upper() or None,
        required_permission=required_permission, requested_by=p.user_id, requested_via=p.channel,
        expires_at=_now() + ttl, trace_id=p.trace_id,
    )
    db.add(row)
    await db.flush()
    await publish(db, "approval.requested", action.scope_id,
                  {"approval_id": str(row.id), "action_type": action.type, "action_hash": row.action_hash}, p.trace_id)
    audit(db, p, "platform.approval_requested", target=str(row.id),
          details={"action_type": action.type, "action_hash": row.action_hash, "subject": action.subject})
    await db.commit()
    await db.refresh(row)
    return row


async def _locked(db: AsyncSession, approval_id: uuid.UUID) -> PlatformApprovalRequest:
    row = await db.scalar(select(PlatformApprovalRequest).where(PlatformApprovalRequest.id == approval_id)
                          .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("Approval not found.")
    return row


async def decide_approval(db: AsyncSession, p: Principal, approval_id: uuid.UUID,
                          decision: Literal["approve", "reject"], *, presented_hash: str,
                          note: str = "") -> PlatformApprovalRequest:
    """`presented_hash` is the hash shown to the approver; the decision applies only to that exact action."""
    if decision not in {"approve", "reject"}:
        raise InvalidInputError("Decision must be approve or reject.")
    row = await _locked(db, approval_id)
    await require(db, p, row.required_permission, row.scope_id, amount=row.amount)
    if row.status != "pending":
        raise ConflictError("This approval is no longer pending.")
    if _now() >= row.expires_at:
        row.status = "expired"
        await db.commit()
        raise ConflictError("This approval expired. Request a new one.")
    if presented_hash != row.action_hash:
        raise ConflictError("The action you reviewed does not match the action waiting for approval.")
    if p.user_id == row.requested_by:
        raise PermissionDeniedError("Separation of duties: you cannot decide your own request.")
    if decision == "approve":
        await assert_separation_of_duties(db, p.user_id, row.subject, row.required_permission)
    row.status = "approved" if decision == "approve" else "rejected"
    row.decided_by, row.decided_at, row.decision_note = p.user_id, _now(), note.strip()[:500] or None
    await publish(db, "approval.decided", row.scope_id,
                  {"approval_id": str(row.id), "status": row.status, "action_hash": row.action_hash}, p.trace_id)
    audit(db, p, f"platform.approval_{row.status}", target=str(row.id),
          details={"action_hash": row.action_hash, "subject": row.subject})
    await db.commit()
    await db.refresh(row)
    return row


async def consume_approval(db: AsyncSession, approval_id: uuid.UUID, action: ActionSpec) -> ApprovalGrant:
    """Use an approval for exactly this action, once. Runs in the caller's transaction (no commit)."""
    row = await _locked(db, approval_id)
    if row.status == "consumed":
        raise ConflictError("This approval was already used.")
    if row.status != "approved":
        raise ConflictError("This action is not approved.")
    if _now() >= row.expires_at:
        raise ConflictError("This approval expired before it was used. Request a new one.")
    if action.hash() != row.action_hash:
        raise ConflictError("The approval covers a different action (parameters or amount changed).")
    row.status, row.consumed_at = "consumed", _now()
    return ApprovalGrant(row.id, row.action_hash, row.decided_by, row.required_permission)
