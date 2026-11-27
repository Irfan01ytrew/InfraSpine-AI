"""QA data is explicitly scoped to an operator-configured tenant and requesting owner."""
import uuid
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class QualityRun(Base):
    __tablename__ = "qa_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','passed','failed','blocked','cancelled','unverified')", name="qa_run_status"),
        Index("ix_qa_runs_scope", "tenant_id", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    suite_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verifies: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    rerun_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"))

class QualityDefect(Base):
    __tablename__ = "qa_defects"
    __table_args__ = (UniqueConstraint("tenant_id", "owner_id", "fingerprint", name="uq_qa_defect_scope"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    test_id: Mapped[str] = mapped_column(String(500), nullable=False)
    module: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"), nullable=False)

class QualityDecision(Base):
    __tablename__ = "qa_decisions"
    __table_args__ = (UniqueConstraint("tenant_id", "owner_id", "snapshot_sha256", name="uq_qa_decision_snapshot"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    report: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
