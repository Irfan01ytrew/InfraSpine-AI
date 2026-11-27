"""Resumable, scoped employee knowledge interviews and reviewed SOP drafts."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KnowledgeInterviewTemplate(Base):
    __tablename__ = "knowledge_interview_templates"
    __table_args__ = (
        CheckConstraint("length(btrim(slug)) BETWEEN 3 AND 80", name="slug_length"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 160", name="title_length"),
        CheckConstraint("length(btrim(description)) BETWEEN 1 AND 800", name="description_length"),
        CheckConstraint(
            "jsonb_typeof(questions) = 'array' AND jsonb_array_length(questions) BETWEEN 1 AND 20",
            name="questions_valid",
        ),
        UniqueConstraint("slug", name="uq_knowledge_interview_templates_slug"),
        Index("ix_knowledge_interview_templates_active", "is_active", "title"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(800), nullable=False)
    questions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class KnowledgeInterviewSession(Base):
    __tablename__ = "knowledge_interview_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'submitted', 'changes_requested', 'approved', 'withdrawn')",
            name="status_valid",
        ),
        CheckConstraint("jsonb_typeof(template_snapshot) = 'object'", name="template_snapshot_object"),
        CheckConstraint("jsonb_typeof(answers) = 'object'", name="answers_object"),
        CheckConstraint("revision_number >= 0", name="revision_nonnegative"),
        CheckConstraint("length(sop_title) <= 180", name="sop_title_length"),
        CheckConstraint("length(sop_body) <= 40000", name="sop_body_length"),
        CheckConstraint("status <> 'submitted' OR submitted_revision IS NOT NULL", name="submission_revision_required"),
        CheckConstraint("submitted_revision IS NULL OR submitted_revision <= revision_number", name="submitted_revision_valid"),
        Index("ix_knowledge_interview_sessions_owner", "owner_user_id", "updated_at"),
        Index("ix_knowledge_interview_sessions_review", "department_group_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_interview_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'in_progress'"))
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    sop_title: Mapped[str] = mapped_column(String(180), nullable=False, server_default=text("''"))
    sop_body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submitted_revision: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class KnowledgeInterviewReview(Base):
    __tablename__ = "knowledge_interview_reviews"
    __table_args__ = (
        UniqueConstraint("session_id", "revision_number", name="uq_knowledge_interview_review_revision"),
        CheckConstraint("decision IN ('approved', 'changes_requested')", name="decision_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        Index("ix_knowledge_interview_reviews_session", "session_id", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_interview_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class KnowledgeInterviewSopRevision(Base):
    """Immutable SOP snapshots; each reviewed version remains inspectable after changes are requested."""

    __tablename__ = "knowledge_interview_sop_revisions"
    __table_args__ = (
        UniqueConstraint("session_id", "revision_number", name="uq_knowledge_interview_sop_revision_number"),
        CheckConstraint("revision_number > 0", name="revision_positive"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(body) BETWEEN 1 AND 40000", name="body_length"),
        CheckConstraint("jsonb_typeof(answers_snapshot) = 'object'", name="answers_snapshot_object"),
        Index("ix_knowledge_interview_sop_revisions_session", "session_id", "revision_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_interview_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    answers_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
