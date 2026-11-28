"""Voice proposals (Step O, slices 2-3): one persisted row per voice note that asked
for a recognised action.

The row carries the proposal from intake through the platform approval to the
unified workflow run (`origin='voice'`). `status` is written by the execution
path only; while the proposal waits for (or has received) its platform approval,
the API derives the display status from the linked approval request, so no
workflow step ever has to write this row to report progress.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

VOICE_PROPOSAL_STATUSES = ("proposed", "approved", "rejected", "expired", "executed", "failed")


class VoiceProposal(Base):
    __tablename__ = "voice_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','approved','rejected','expired','executed','failed')",
            name="ck_voice_proposals_status_valid",
        ),
        Index("ix_voice_proposals_group", "group_id", "created_at"),
        Index("ix_voice_proposals_approval", "approval_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Department (access group) the voice note acts in; the workflow run's scope.
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False)
    # Platform scope of that department (permissions, approvals, executions).
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    intent_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[dict] = mapped_column(JSONB, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default=text("'proposed'"))
    transcript_excerpt: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=text("''"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    wf_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wf_runs.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_result: Mapped[dict | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
