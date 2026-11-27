"""Persistent RFP coverage cases with evidence-backed findings and scoped review."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProposalCoverageCase(Base):
    __tablename__ = "proposal_coverage_cases"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', 'approved', 'withdrawn')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_valid"),
        CheckConstraint("revision_number >= 0", name="revision_nonnegative"),
        Index("ix_proposal_coverage_cases_owner", "owner_user_id", "updated_at"),
        Index("ix_proposal_coverage_cases_review", "department_group_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'draft'"))
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProposalCoverageRequirement(Base):
    __tablename__ = "proposal_coverage_requirements"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 30", name="position_valid"),
        CheckConstraint("length(btrim(requirement)) BETWEEN 5 AND 2000", name="requirement_valid"),
        CheckConstraint("length(answer) <= 8000", name="answer_valid"),
        CheckConstraint("jsonb_typeof(citations) = 'array' AND jsonb_array_length(citations) <= 10", name="citations_valid"),
        CheckConstraint("jsonb_typeof(origin_evidence) = 'array' AND jsonb_array_length(origin_evidence) <= 5", name="origin_evidence_valid"),
        CheckConstraint("assessment IN ('not_assessed', 'meets', 'partial', 'gap')", name="assessment_valid"),
        CheckConstraint("length(assessment_rationale) <= 2000", name="assessment_rationale_valid"),
        CheckConstraint(
            "assessment = 'not_assessed' OR (assessed_by_user_id IS NOT NULL AND assessed_at IS NOT NULL "
            "AND length(btrim(assessment_rationale)) BETWEEN 3 AND 2000)",
            name="assessment_complete",
        ),
        UniqueConstraint("case_id", "position", name="uq_proposal_coverage_requirements_position"),
        Index("ix_proposal_coverage_requirements_case", "case_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposal_coverage_cases.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement: Mapped[str] = mapped_column(String(2000), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    insufficient_evidence: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    citations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    origin_evidence: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    retrieved_chunks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assessment: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'not_assessed'"))
    assessment_rationale: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"))
    assessed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProposalCoverageReview(Base):
    """Append-only review of the submitted case snapshot for an explicit revision."""

    __tablename__ = "proposal_coverage_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'changes_requested')", name="decision_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_valid"),
        CheckConstraint("revision_number > 0", name="revision_positive"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
        UniqueConstraint("case_id", "revision_number", name="uq_proposal_coverage_reviews_revision"),
        Index("ix_proposal_coverage_reviews_case", "case_id", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposal_coverage_cases.id", ondelete="RESTRICT"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
