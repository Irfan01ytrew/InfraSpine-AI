"""Durable recovery records. All resource lookups require tenant AND owner scope."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Tenant(Base, Identity):
    __tablename__ = "recovery_tenants"
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = "recovery_members"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Target(Base, Identity):
    __tablename__ = "recovery_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "adapter", "resource", name="uq_recovery_target"),
        UniqueConstraint("id", "tenant_id", name="recovery_target_tenant"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    adapter: Mapped[str] = mapped_column(String(40))
    resource: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold: Mapped[int] = mapped_column(Integer, default=1)
    window_seconds: Mapped[int] = mapped_column(Integer, default=300)
    sla_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    check_interval: Mapped[int] = mapped_column(Integer, default=60)
    next_check: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    healthy: Mapped[bool | None] = mapped_column(Boolean)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Playbook(Base, Identity):
    __tablename__ = "recovery_playbooks"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="recovery_book_tenant"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    adapter: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(32))
    risk: Mapped[str] = mapped_column(String(16))
    preapproved: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=15)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=5)
    verify_samples: Mapped[int] = mapped_column(Integer, default=2)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Incident(Base, Identity):
    __tablename__ = "recovery_incidents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", "fingerprint", name="uq_recovery_fingerprint"),
        UniqueConstraint("id", "tenant_id", name="recovery_incident_tenant"),
        ForeignKeyConstraint(["target_id", "tenant_id"], ["recovery_targets.id", "recovery_targets.tenant_id"], name="recovery_incident_target_scope"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_targets.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(24), default="detected", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    correlation_key: Mapped[str | None] = mapped_column(String(120))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sla_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    diagnosis: Mapped[dict] = mapped_column(JSONB, default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    audit_head: Mapped[str] = mapped_column(String(64), default="0" * 64)
    audit_seq: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base, Identity):
    __tablename__ = "recovery_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", "event_key", name="uq_recovery_event"),
        ForeignKeyConstraint(["target_id", "tenant_id"], ["recovery_targets.id", "recovery_targets.tenant_id"], name="recovery_event_target_scope"),
        ForeignKeyConstraint(["incident_id", "tenant_id"], ["recovery_incidents.id", "recovery_incidents.tenant_id"], name="recovery_event_incident_scope"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_targets.id"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recovery_incidents.id"))
    event_key: Mapped[str] = mapped_column(String(120))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    disposition: Mapped[str] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Timeline(Base, Identity):
    __tablename__ = "recovery_timeline"
    __table_args__ = (
        UniqueConstraint("incident_id", "sequence", name="uq_recovery_sequence"),
        ForeignKeyConstraint(["incident_id", "tenant_id"], ["recovery_incidents.id", "recovery_incidents.tenant_id"], name="recovery_timeline_scope"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_incidents.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(32))


class Command(Base, Identity):
    __tablename__ = "recovery_commands"
    __table_args__ = (
        UniqueConstraint("incident_id", "request_key", name="uq_recovery_command_key"),
        ForeignKeyConstraint(["incident_id", "tenant_id"], ["recovery_incidents.id", "recovery_incidents.tenant_id"], name="recovery_command_scope"),
        ForeignKeyConstraint(["target_id", "tenant_id"], ["recovery_targets.id", "recovery_targets.tenant_id"], name="recovery_command_target_scope"),
        ForeignKeyConstraint(["playbook_id", "tenant_id"], ["recovery_playbooks.id", "recovery_playbooks.tenant_id"], name="recovery_command_book_scope"),
        Index(
            "uq_recovery_active_command", "incident_id", unique=True,
            postgresql_where=text("status IN ('proposed','awaiting_approval','queued','executing','verifying')"),
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_tenants.id"), index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_incidents.id"), index=True)
    playbook_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_playbooks.id"))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_targets.id"))
    generation: Mapped[int] = mapped_column(Integer)
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approval_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_digest: Mapped[str | None] = mapped_column(String(64))
    action_digest: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="proposed", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    successes: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))


TABLES = [Tenant.__table__, Member.__table__, Target.__table__, Playbook.__table__, Incident.__table__,
          Event.__table__, Timeline.__table__, Command.__table__]
