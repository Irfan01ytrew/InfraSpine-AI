"""Persisted tenant boundaries and immutable provenance for process discovery."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Tenant(Base, Identity):
    __tablename__ = "pi_tenants"
    name: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class Membership(Base):
    __tablename__ = "pi_memberships"
    __table_args__ = (CheckConstraint("role IN ('viewer','analyst','steward')", name="role"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pi_tenants.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))


class Process(Base, Identity):
    __tablename__ = "pi_processes"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), CheckConstraint("revision >= 0", name="revision"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pi_tenants.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(2000), default="")
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Source(Base, Identity):
    __tablename__ = "pi_sources"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id"], ["pi_processes.tenant_id", "pi_processes.id"], ondelete="CASCADE"),
        UniqueConstraint("tenant_id", "process_id", "id", name="uq_pi_sources_process_identity"),
        UniqueConstraint("tenant_id", "id", name="uq_pi_sources_identity"),
        CheckConstraint("kind IN ('erp','crm','hrms','accounting','email','document','ticket','workflow','api','event_log')", name="kind"),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24))
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
    cursor: Mapped[str | None] = mapped_column(String(256))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceGrant(Base):
    __tablename__ = "pi_source_grants"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["pi_sources.tenant_id", "pi_sources.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["pi_memberships.tenant_id", "pi_memberships.user_id"], ondelete="CASCADE"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class Event(Base, Identity):
    __tablename__ = "pi_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id", "source_id"], ["pi_sources.tenant_id", "pi_sources.process_id", "pi_sources.id"], ondelete="CASCADE"),
        UniqueConstraint("source_id", "external_id"),
        Index("ix_pi_events_case", "tenant_id", "process_id", "source_id", "case_id", "occurred_at"),
        CheckConstraint("finished_at IS NULL OR finished_at >= occurred_at", name="duration"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    external_id: Mapped[str] = mapped_column(String(160))
    case_id: Mapped[str] = mapped_column(String(160))
    activity: Mapped[str] = mapped_column(String(160))
    actor: Mapped[str | None] = mapped_column(String(160))
    department: Mapped[str | None] = mapped_column(String(160))
    outcome: Mapped[str | None] = mapped_column(String(80))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_ref: Mapped[str] = mapped_column(String(320))
    payload_hash: Mapped[str] = mapped_column(String(64))


class Trace(Base, Identity):
    __tablename__ = "pi_traces"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id", "source_id"], ["pi_sources.tenant_id", "pi_sources.process_id", "pi_sources.id"], ondelete="CASCADE"),
        UniqueConstraint("source_id", "case_id"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    case_id: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict] = mapped_column(JSONB)


class Baseline(Base, Identity):
    __tablename__ = "pi_baselines"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id", "source_id"], ["pi_sources.tenant_id", "pi_sources.process_id", "pi_sources.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('proposed','approved','rejected')", name="status"),
        CheckConstraint("reviewed_by IS NULL OR reviewed_by <> created_by", name="independent_review"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    origin: Mapped[str] = mapped_column(String(24), default="human")
    data: Mapped[dict] = mapped_column(JSONB)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Model(Base, Identity):
    __tablename__ = "pi_models"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id"], ["pi_processes.tenant_id", "pi_processes.id"], ondelete="CASCADE"),
        UniqueConstraint("process_id", "owner_id", "input_hash"),
        Index("ix_pi_models_scope", "tenant_id", "process_id", "owner_id", "created_at"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    source_ids: Mapped[list] = mapped_column(JSONB)
    data: Mapped[dict] = mapped_column(JSONB)
    review_status: Mapped[str] = mapped_column(String(16), default="unreviewed")


class Job(Base, Identity):
    __tablename__ = "pi_jobs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "process_id"], ["pi_processes.tenant_id", "pi_processes.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('queued','processing','completed','failed')", name="status"),
        Index("ix_pi_jobs_queue", "status", "lease_until", "created_at"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    process_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pi_models.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(String(64))


TABLES = [Tenant.__table__, Membership.__table__, Process.__table__, Source.__table__, SourceGrant.__table__, Event.__table__, Trace.__table__, Baseline.__table__, Model.__table__, Job.__table__]
