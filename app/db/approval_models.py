"""Approval authority: how much a position (or one named person) may approve per kind of financial request.

Authority is configured independently of software roles and job titles. A person's own limit overrides the
limit of their position. Nobody, administrators included, has financial approval authority until a limit
is configured, and nobody may approve their own request.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

FINANCIAL_CATEGORIES = ("purchase", "expense")


class ApprovalLimit(Base):
    __tablename__ = "approval_limits"
    __table_args__ = (
        CheckConstraint("category IN ('purchase', 'expense')", name="category_valid"),
        CheckConstraint("(position_id IS NULL) <> (user_id IS NULL)", name="one_subject"),
        CheckConstraint("max_amount >= 0", name="amount_nonnegative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_valid"),
        Index("uq_approval_limits_position", "category", "position_id", unique=True, postgresql_where=text("is_active AND position_id IS NOT NULL")),
        Index("uq_approval_limits_user", "category", "user_id", unique=True, postgresql_where=text("is_active AND user_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    max_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


APPROVER_KINDS = ("manager", "position", "person")
STEP_STATUSES = ("waiting", "pending", "approved", "rejected", "cancelled")


class ApprovalChainStep(Base):
    """One step of the configured approval chain for a kind of financial request.

    `manager` means the requester's manager (their reporting line), `position` any active holder of a
    position, `person` one named person. A step with `min_amount` only applies at or above that amount.
    A step marked `parallel_with_previous` runs in the same stage as the step before it; a stage's steps are
    decided at the same time and `stage_mode` says whether all of them ('all') or any one ('any') must approve.
    """

    __tablename__ = "approval_chain_steps"
    __table_args__ = (
        CheckConstraint("category IN ('purchase', 'expense')", name="category_valid"),
        CheckConstraint("approver_kind IN ('manager', 'position', 'person')", name="kind_valid"),
        CheckConstraint(
            "(approver_kind = 'manager' AND position_id IS NULL AND user_id IS NULL)"
            " OR (approver_kind = 'position' AND position_id IS NOT NULL AND user_id IS NULL)"
            " OR (approver_kind = 'person' AND user_id IS NOT NULL AND position_id IS NULL)",
            name="approver_matches_kind",
        ),
        CheckConstraint("step_order BETWEEN 1 AND 20", name="order_valid"),
        CheckConstraint("stage_mode IN ('all', 'any')", name="stage_mode_valid"),
        CheckConstraint("min_amount IS NULL OR min_amount >= 0", name="min_amount_nonnegative"),
        CheckConstraint("length(btrim(label)) BETWEEN 2 AND 80", name="label_valid"),
        Index("uq_approval_chain_steps_order", "category", "step_order", unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    step_order: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    approver_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    parallel_with_previous: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    stage_mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'all'"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RequestApprovalStep(Base):
    """The approval steps of one request, copied from the chain when it was submitted so later policy
    changes do not move in-flight requests. Exactly one step is `pending` while the request is open."""

    __tablename__ = "request_approval_steps"
    __table_args__ = (
        CheckConstraint("status IN ('waiting', 'pending', 'approved', 'rejected', 'cancelled')", name="status_valid"),
        CheckConstraint("stage_mode IN ('all', 'any')", name="stage_mode_valid"),
        CheckConstraint("approver_kind IN ('manager', 'position', 'person')", name="kind_valid"),
        CheckConstraint("comment IS NULL OR length(comment) <= 2000", name="comment_length"),
        Index("uq_request_approval_steps_order", "request_id", "step_order", unique=True),
        Index("ix_request_approval_steps_pending", "status", "approver_user_id", "approver_position_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_requests.id", ondelete="CASCADE"), nullable=False)
    step_order: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    approver_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Steps of one stage are decided at the same time: all must approve ('all') or any one ('any').
    stage: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
    stage_mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'all'"))
    # Resolved when the request was submitted: the manager or named person; or the position whose holders may act.
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approver_position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    final: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_email: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(String(2000))
    # When the step started waiting for a decision, and reminders sent since.
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a delegate decided the step for the approver who was away.
    on_behalf_of_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    on_behalf_of_email: Mapped[str | None] = mapped_column(String(320))
    # Set once, when the step was escalated after its last reminder.
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Budget(Base):
    """A department's spending budget for a period, for purchase requests, expense claims or both ('all').

    Spend is derived from the department's financial requests: approved (closed as approved in the period) and
    committed (still being approved). `enforcement` decides what happens when a final approval would exceed it:
    'warn' allows it and records the overrun; 'block' refuses the approval.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("category IN ('purchase', 'expense', 'all')", name="category_valid"),
        CheckConstraint("enforcement IN ('warn', 'block')", name="enforcement_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_valid"),
        CheckConstraint("period_end >= period_start", name="period_valid"),
        CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name="name_valid"),
        Index("ix_budgets_department_period", "department_group_id", "period_start", "period_end"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    department_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    enforcement: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'warn'"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ApprovalDelegation(Base):
    """While the delegator is away (starts_on..ends_on), the delegate decides approval steps for them."""

    __tablename__ = "approval_delegations"
    __table_args__ = (
        CheckConstraint("delegator_user_id <> delegate_user_id", name="not_self"),
        CheckConstraint("ends_on >= starts_on", name="period_valid"),
        Index("ix_approval_delegations_delegate", "delegate_user_id", "starts_on", "ends_on"),
        Index("ix_approval_delegations_delegator", "delegator_user_id", "starts_on", "ends_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delegator_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    delegate_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalEmail(Base):
    """Email outbox for approval notices; sent, retried and cancelled by the approval reminder worker."""

    __tablename__ = "approval_emails"
    __table_args__ = (
        CheckConstraint("kind IN ('pending', 'reminder', 'escalation')", name="kind_valid"),
        CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'cancelled')", name="status_valid"),
        Index("ix_approval_emails_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_requests.id", ondelete="CASCADE"), nullable=False)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
