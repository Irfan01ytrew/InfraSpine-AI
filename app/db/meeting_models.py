"""Auditable meeting minutes and approval-gated action items."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingArtifact(Base):
    __tablename__ = "meeting_artifacts"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', 'approved')", name="status_valid"),
        CheckConstraint("current_revision > 0", name="revision_positive"),
        CheckConstraint("source_document_version > 0", name="source_version_positive"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="source_digest_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("(status = 'submitted') = (submitted_revision_id IS NOT NULL)", name="submission_pair_valid"),
        Index("ix_meeting_artifacts_source_created", "source_document_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    owner_email: Mapped[str] = mapped_column(String(320), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'draft'"))
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    submitted_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meeting_revisions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeetingRevision(Base):
    """Immutable snapshots: editing creates a new version and never overwrites old minutes."""

    __tablename__ = "meeting_revisions"
    __table_args__ = (
        UniqueConstraint("meeting_id", "version", name="uq_meeting_revisions_meeting_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(minutes_markdown) <= 40000", name="minutes_length"),
        CheckConstraint("jsonb_typeof(decisions) = 'array' AND jsonb_array_length(decisions) <= 30", name="decisions_bounded"),
        CheckConstraint("jsonb_typeof(proposed_tasks) = 'array' AND jsonb_array_length(proposed_tasks) <= 30", name="tasks_bounded"),
        Index("ix_meeting_revisions_meeting_created", "meeting_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meeting_artifacts.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    editor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    editor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    minutes_markdown: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    decisions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    proposed_tasks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeetingReview(Base):
    """Append-only approval or change request for a submitted revision."""

    __tablename__ = "meeting_reviews"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_meeting_reviews_revision"),
        CheckConstraint("decision IN ('approved', 'changes_requested')", name="decision_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        Index("ix_meeting_reviews_meeting_reviewed", "meeting_id", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meeting_artifacts.id", ondelete="RESTRICT"), nullable=False)
    revision_id: Mapped[int] = mapped_column(ForeignKey("meeting_revisions.id", ondelete="RESTRICT"), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reviewer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeetingActionTask(Base):
    """An action item instantiated only from an approved meeting revision."""

    __tablename__ = "meeting_action_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'completed')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="body_length"),
        CheckConstraint("jsonb_typeof(evidence_chunk_ids) = 'array' AND jsonb_array_length(evidence_chunk_ids) BETWEEN 1 AND 10", name="evidence_valid"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL AND completed_by IS NOT NULL)", name="completion_pair_valid"),
        Index("ix_meeting_action_tasks_assignee_created", "assignee_user_id", "created_at"),
        Index("ix_meeting_action_tasks_meeting_created", "meeting_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meeting_artifacts.id", ondelete="RESTRICT"), nullable=False)
    revision_id: Mapped[int] = mapped_column(ForeignKey("meeting_revisions.id", ondelete="RESTRICT"), nullable=False)
    source_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
    evidence_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
