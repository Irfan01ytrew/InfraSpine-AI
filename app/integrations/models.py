"""Tenant/owner-bound integration storage; payloads and credentials are encrypted."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Connector(Base):
    __tablename__ = "integration_connectors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "id", name="uq_integration_connector_scope"),
        ForeignKeyConstraint(["tenant_id", "owner_id"], ["company_graph_members.tenant_id", "company_graph_members.user_id"], ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="version"),
        Index("ix_integration_connector_scope", "tenant_id", "owner_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(1000))
    config: Mapped[dict] = mapped_column(JSONB)
    credentials: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    health: Mapped[str] = mapped_column(String(32), default="unverified")
    last_error: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    circuit_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "owner_id", "connector_id"], ["integration_connectors.tenant_id", "integration_connectors.owner_id", "integration_connectors.id"], ondelete="RESTRICT"),
        UniqueConstraint("connector_id", "idempotency_key", name="uq_integration_job_key"),
        CheckConstraint("status IN ('pending_approval','queued','processing','dispatching','completed','dead_letter','uncertain','cancelled')", name="status"),
        CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts"),
        Index("ix_integration_job_claim", "status", "available_at"),
        Index("ix_integration_job_scope", "tenant_id", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID)
    connector_version: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    last_error: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationRecord(Base):
    __tablename__ = "integration_records"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "owner_id", "connector_id"], ["integration_connectors.tenant_id", "integration_connectors.owner_id", "integration_connectors.id"], ondelete="RESTRICT"),
        UniqueConstraint("connector_id", "external_id", name="uq_integration_record_external"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID)
    external_id: Mapped[str] = mapped_column(String(240))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "owner_id", "connector_id"], ["integration_connectors.tenant_id", "integration_connectors.owner_id", "integration_connectors.id"], ondelete="RESTRICT"),
        UniqueConstraint("connector_id", "external_id", name="uq_integration_event_external"),
        Index("ix_integration_event_scope", "tenant_id", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID)
    external_id: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(80))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    target_module: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="received")
    consumer_reference: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthAttempt(Base):
    __tablename__ = "integration_oauth_attempts"
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "owner_id", "connector_id"], ["integration_connectors.tenant_id", "integration_connectors.owner_id", "integration_connectors.id"], ondelete="CASCADE"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    verifier: Mapped[dict] = mapped_column(JSONB)
    connector_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class WhatsAppDispatch(Base):
    """Delivery evidence for the existing owner-bound Cloud connection and inbox."""
    __tablename__ = "integration_whatsapp_dispatches"
    __table_args__ = (Index("uq_integration_whatsapp_dispatch_progress_id", "progress_message_id", unique=True),)
    inbound_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_whatsapp_inbound.id", ondelete="CASCADE"), primary_key=True)
    phase: Mapped[str] = mapped_column(String(24), default="waiting")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    incoming_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    delivery_status: Mapped[str | None] = mapped_column(String(24))
    receipt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_message_id: Mapped[str | None] = mapped_column(String(160))
    progress_status: Mapped[str | None] = mapped_column(String(24))
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppArtifactDispatch(Base):
    """Crash-fenced delivery state for one owner-bound WhatsApp output artifact."""
    __tablename__ = "integration_whatsapp_artifacts"
    __table_args__ = (
        UniqueConstraint("inbound_id", "artifact_key", name="uq_integration_whatsapp_artifact_key"),
        UniqueConstraint("provider_message_id", name="uq_integration_whatsapp_artifact_message_id"),
        Index("ix_integration_whatsapp_artifacts_inbound", "inbound_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inbound_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_whatsapp_inbound.id", ondelete="CASCADE"), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    media_id: Mapped[str | None] = mapped_column(String(160))
    provider_message_id: Mapped[str | None] = mapped_column(String(160))
    delivery_status: Mapped[str | None] = mapped_column(String(24))
    last_error: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
