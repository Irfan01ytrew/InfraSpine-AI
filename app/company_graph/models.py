"""Tenant-bound graph storage. Composite FKs prevent cross-company edges."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GraphTenant(Base):
    __tablename__ = "company_graph_tenants"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphMember(Base):
    __tablename__ = "company_graph_members"
    __table_args__ = (CheckConstraint("role IN ('owner','approver','editor','viewer')", name="role"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    departments: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    all_departments: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class GraphRecord(Base):
    __tablename__ = "company_graph_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_cg_record_tenant_id"),
        UniqueConstraint("tenant_id", "external_key", name="uq_cg_record_external_key"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["company_graph_records.tenant_id", "company_graph_records.id"], name="fk_cg_source"),
        ForeignKeyConstraint(["tenant_id", "target_id"], ["company_graph_records.tenant_id", "company_graph_records.id"], name="fk_cg_target"),
        ForeignKeyConstraint(["tenant_id", "department_id"], ["company_graph_records.tenant_id", "company_graph_records.id"], name="fk_cg_department"),
        CheckConstraint("(record_type = 'entity' AND source_id IS NULL AND target_id IS NULL) OR (record_type = 'relationship' AND source_id IS NOT NULL AND target_id IS NOT NULL AND source_id <> target_id)", name="shape"),
        CheckConstraint("visibility IN ('tenant','department','restricted')", name="visibility"),
        CheckConstraint("visibility <> 'department' OR department_id IS NOT NULL", name="department_visibility"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_cg_outgoing", "tenant_id", "source_id", "kind"),
        Index("ix_cg_incoming", "tenant_id", "target_id", "kind"),
        Index("ix_cg_list", "tenant_id", "record_type", "kind", "id"),
        Index("ix_cg_department", "tenant_id", "department_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"))
    record_type: Mapped[str] = mapped_column(String(16))
    external_key: Mapped[str] = mapped_column(String(220))
    kind: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(240))
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    visibility: Mapped[str] = mapped_column(String(16), default="restricted")
    readers: Mapped[list] = mapped_column(JSONB, default=list)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1)
    inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphChange(Base):
    __tablename__ = "company_graph_changes"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected')", name="status"),
        CheckConstraint("change_type IN ('record','member')", name="type"),
        Index("ix_cg_pending", "tenant_id", "status", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"))
    change_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID)
    payload: Mapped[dict] = mapped_column(JSONB)
    base_version: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class GraphEvent(Base):
    """Transactional outbox and immutable revision journal; never contains credentials."""
    __tablename__ = "company_graph_events"
    __table_args__ = (UniqueConstraint("tenant_id", "sequence", name="uq_cg_event_sequence"), Index("ix_cg_event_subject", "tenant_id", "subject_id", "sequence"))
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID)
    event_type: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphSource(Base):
    __tablename__ = "company_graph_sources"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_cg_source_tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    stale_after_days: Mapped[int] = mapped_column(Integer, default=90)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphSync(Base):
    __tablename__ = "company_graph_syncs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "idempotency_key", name="uq_cg_sync_key"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["company_graph_sources.tenant_id", "company_graph_sources.id"], name="fk_cg_sync_source"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company_graph_tenants.id", ondelete="CASCADE"))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
