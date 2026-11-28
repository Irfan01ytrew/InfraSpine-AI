"""Step M (slice 2): persistence models for decision records.

One decision per row in ``decision_records``; ``decision_events`` is the
append-only lifecycle trail (recorded / superseded / revoked / reviewed).
Evidence is stored as JSONB references (kind + ref + label) only — never
copied document text — so a reader who may not read the evidence never
learns its contents from the decision. History is append-only: a decision
is never edited or deleted; supersession and revocation change only the
status columns, and the old record stays readable.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DECISION_RECORD_KINDS = ("policy", "finance", "operations", "supplier", "people", "technology", "other")
DECISION_RECORD_STATUSES = ("active", "superseded", "revoked", "expired")
DECISION_EVENT_TYPES = ("recorded", "superseded", "revoked", "reviewed")


class DecisionRecordRow(Base):
    """One organizational decision, org-scoped and append-only (Step M plan §2)."""

    __tablename__ = "decision_records"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('policy', 'finance', 'operations', 'supplier', 'people', 'technology', 'other')",
            name="ck_decision_records_kind_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'revoked', 'expired')",
            name="ck_decision_records_status_valid",
        ),
        CheckConstraint("jsonb_typeof(alternatives_considered) = 'array'",
                        name="ck_decision_records_alternatives_are_array"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'",
                        name="ck_decision_records_evidence_is_array"),
        CheckConstraint("jsonb_typeof(supersedes) = 'array'",
                        name="ck_decision_records_supersedes_is_array"),
        CheckConstraint("jsonb_typeof(depends_on) = 'array'",
                        name="ck_decision_records_depends_on_is_array"),
        CheckConstraint(
            "(status = 'superseded' AND superseded_by IS NOT NULL AND superseded_at IS NOT NULL)"
            " OR (status <> 'superseded' AND superseded_by IS NULL AND superseded_at IS NULL)",
            name="ck_decision_records_superseded_details_valid",
        ),
        Index("ix_decision_records_scope_status", "scope_id", "status"),
        Index("ix_decision_records_scope_decided", "scope_id", "decided_at"),
        Index("ix_decision_records_fingerprint", "fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'other'"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    decided_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    alternatives_considered: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    supersedes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    depends_on: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    review_at: Mapped[date | None] = mapped_column(Date)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decision_records.id", ondelete="SET NULL"))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    needs_review: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DecisionEventRow(Base):
    """Append-only lifecycle trail for decision records (Step M plan §3)."""

    __tablename__ = "decision_events"
    __table_args__ = (
        CheckConstraint(
            "event IN ('recorded', 'superseded', 'revoked', 'reviewed')",
            name="ck_decision_events_event_valid",
        ),
        Index("ix_decision_events_decision_created", "decision_id", "created_at"),
        Index("ix_decision_events_event_created", "event", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decision_records.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
