"""Persisted twin-driven proposals (Step K v2 slice 6).

One row per proposal, per graph tenant. A proposal records that
one twin finding (a drill-reconciliation queue row, or a
simulation finding) was turned into an approval-gated action
proposal: the exact canonical action (:class:`ActionSpec`, bound
by its hash), who must approve it, the platform approval id, the
``execute_once`` execution id once dispatched, and the lifecycle
status.

The lifecycle is: ``draft`` → ``pending_approval`` → ``approved``
| ``rejected`` | ``expired`` | ``cancelled`` → ``dispatched`` →
``verified`` | ``failed``. The platform approval row is the source
of truth for approve/reject/expire; this row mirrors it so the
proposal inbox can be listed without joining the approval table.
The execution row is the source of truth for dispatch/verify.

A proposal never executes itself: only an approved proposal may
be dispatched, only through ``execute_once`` with read-back
verification (see :mod:`app.digital_twin.proposals_records`), and
the executor may be neither the proposer nor the approver. The
finding summary carries the money difference for owner-only
findings (redacted for non-owners at read time); the action
params never carry money.

Rows are never edited except for the lifecycle transitions
(status / approval_id / execution_id / external_ref /
decided_by). Old rows leave only through the retention rule
(terminal proposals only — a pending or approved proposal is
never pruned — newest 100 per tenant, nothing older than 90
days, enforced by the service, not the DB). Dropping this table
loses proposal history, not the twin: proposals are re-derived
from the stored findings.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: Lifecycle statuses. ``draft`` exists only between the row insert
#: and the approval request in the same service call; a stored row
#: is normally already ``pending_approval``.
PROPOSAL_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "dispatched",
    "verified",
    "failed",
)

#: Terminal statuses — the only ones the retention prune may delete.
TERMINAL_PROPOSAL_STATUSES = ("rejected", "expired", "cancelled", "verified", "failed")


class TwinProposal(Base):
    __tablename__ = "twin_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "proposal_fingerprint", name="uq_twin_proposals_tenant_fingerprint"),
        CheckConstraint("proposal_fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_twin_proposals_fingerprint_hex"),
        CheckConstraint("action_hash ~ '^[0-9a-f]{64}$'", name="ck_twin_proposals_action_hash_hex"),
        CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired', "
            "'cancelled', 'dispatched', 'verified', 'failed')",
            name="ck_twin_proposals_status",
        ),
        CheckConstraint(
            "source IN ('reconciliation', 'simulation')",
            name="ck_twin_proposals_source",
        ),
        Index("ix_twin_proposals_tenant_created", "tenant_id", "created_at"),
        Index("ix_twin_proposals_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    finding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_permission: Mapped[str] = mapped_column(String(128), nullable=False)
    duty: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    proposed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["PROPOSAL_STATUSES", "TERMINAL_PROPOSAL_STATUSES", "TwinProposal"]
