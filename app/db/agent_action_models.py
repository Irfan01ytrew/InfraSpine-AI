"""Persisted agent action proposals with independent human approval and idempotent execution."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ACTION_PENDING_APPROVAL = "pending_approval"
ACTION_APPROVED = "approved"
ACTION_REJECTED = "rejected"
ACTION_EXPIRED = "expired"
ACTION_EXECUTED = "executed"

ACTION_STATUSES = frozenset({
    ACTION_PENDING_APPROVAL,
    ACTION_APPROVED,
    ACTION_REJECTED,
    ACTION_EXPIRED,
    ACTION_EXECUTED,
})

ACTION_TYPES = frozenset({
    "webhook_dispatch",
    "document_publish",
    "notification_broadcast",
})


class AgentActionProposal(Base):
    """An agent-proposed action requiring out-of-model human authorization before execution."""

    __tablename__ = "agent_action_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'expired', 'executed')",
            name="ck_agent_action_status_valid",
        ),
        CheckConstraint(
            "action_type IN ('webhook_dispatch', 'document_publish', 'notification_broadcast')",
            name="ck_agent_action_type_valid",
        ),
        CheckConstraint(
            "char_length(btrim(rationale)) BETWEEN 3 AND 1000",
            name="ck_agent_action_rationale_valid",
        ),
        CheckConstraint(
            "(status = 'pending_approval' AND approver_user_id IS NULL AND reviewed_at IS NULL AND approval_rationale IS NULL) OR "
            "(status IN ('approved', 'rejected', 'executed') AND approver_user_id IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(status = 'expired')",
            name="ck_agent_action_review_lifecycle",
        ),
        CheckConstraint(
            "(status = 'executed' AND executed_at IS NOT NULL AND execution_result IS NOT NULL) OR "
            "(status <> 'executed' AND executed_at IS NULL AND execution_result IS NULL)",
            name="ck_agent_action_execution_lifecycle",
        ),
        UniqueConstraint("idempotency_key", name="uq_agent_action_proposals_idempotency_key"),
        Index("ix_agent_action_proposals_status_created", "status", "requested_at"),
        Index("ix_agent_action_proposals_dept_status", "department_id", "status"),
        Index("ix_agent_action_proposals_requester", "requester_user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_groups.id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(String(1000), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending_approval'")
    )
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approval_rationale: Mapped[str | None] = mapped_column(String(1000))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_result: Mapped[dict | None] = mapped_column(JSONB)
