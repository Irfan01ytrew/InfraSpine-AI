"""Governance storage. Composite foreign keys make tenant boundaries structural."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def timestamp():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Tenant(Base):
    __tablename__ = "security_tenants"
    id: Mapped[uuid.UUID] = uid()
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    policy_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    audit_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    audit_head: Mapped[str] = mapped_column(String(64), default="0" * 64, server_default="0" * 64)
    created_at: Mapped[datetime] = timestamp()


class Department(Base):
    __tablename__ = "security_departments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name", name="uq_security_departments_tenant_name"),
        ForeignKeyConstraint(["tenant_id", "parent_id"], ["security_departments.tenant_id", "security_departments.id"]),
        CheckConstraint("id <> parent_id", name="not_self_parent"),
    )
    id: Mapped[uuid.UUID] = uid()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"))
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Member(Base):
    __tablename__ = "security_members"
    __table_args__ = (
        # A principal's data ownership cannot be moved into a different tenant by
        # deleting/recreating a membership. Membership rows are retained on revoke.
        UniqueConstraint("user_id"),
        ForeignKeyConstraint(["tenant_id", "department_id"], ["security_departments.tenant_id", "security_departments.id"]),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    position_title: Mapped[str] = mapped_column(String(120), default="", server_default="")
    all_departments: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = timestamp()


class Role(Base):
    __tablename__ = "security_roles"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name", name="uq_security_roles_tenant_name"))
    id: Mapped[uuid.UUID] = uid()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"))
    name: Mapped[str] = mapped_column(String(120))
    permissions: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = timestamp()


class Assignment(Base):
    __tablename__ = "security_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["security_members.tenant_id", "security_members.user_id"]),
        ForeignKeyConstraint(["tenant_id", "role_id"], ["security_roles.tenant_id", "security_roles.id"]),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyVersion(Base):
    __tablename__ = "security_policy_versions"
    __table_args__ = (CheckConstraint("version > base_version AND base_version >= 0", name="version_order"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[list] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = timestamp()


class AgentIdentity(Base):
    __tablename__ = "security_agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "owner_user_id"], ["security_members.tenant_id", "security_members.user_id"]),
        CheckConstraint("max_calls BETWEEN 1 AND 1000 AND calls_used >= 0", name="budget"),
        CheckConstraint("window_seconds BETWEEN 60 AND 86400 AND timeout_seconds BETWEEN 1 AND 120", name="limits"),
    )
    id: Mapped[uuid.UUID] = uid()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(120))
    tools: Mapped[list] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    max_calls: Mapped[int] = mapped_column(Integer, default=20)
    window_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    calls_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    window_started_at: Mapped[datetime] = timestamp()


class Resource(Base):
    __tablename__ = "security_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"), UniqueConstraint("kind", "external_id"),
        ForeignKeyConstraint(["tenant_id", "department_id"], ["security_departments.tenant_id", "security_departments.id"]),
        CheckConstraint("kind IN ('document', 'agent')", name="kind"),
        CheckConstraint("classification IN ('internal','confidential','restricted')", name="classification"),
    )
    id: Mapped[uuid.UUID] = uid()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"))
    kind: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    classification: Mapped[str] = mapped_column(String(24), default="internal")
    label: Mapped[str] = mapped_column(String(160))


class ActionRequest(Base):
    __tablename__ = "security_action_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "requester_user_id"], ["security_members.tenant_id", "security_members.user_id"]),
        ForeignKeyConstraint(["tenant_id", "resource_id"], ["security_resources.tenant_id", "security_resources.id"]),
        ForeignKeyConstraint(["tenant_id", "agent_id"], ["security_agents.tenant_id", "security_agents.id"]),
        CheckConstraint("status IN ('pending','ready','rejected','executed','expired')", name="status"),
        CheckConstraint("risk IN ('low','medium','high','critical') AND required_approvals BETWEEN 0 AND 2", name="risk"),
    )
    id: Mapped[uuid.UUID] = uid()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"), index=True)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    risk: Mapped[str] = mapped_column(String(16))
    required_approvals: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = timestamp()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)


class Decision(Base):
    __tablename__ = "security_approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "request_id"], ["security_action_requests.tenant_id", "security_action_requests.id"]),
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["security_members.tenant_id", "security_members.user_id"]),
        CheckConstraint("decision IN ('approve','reject')", name="decision"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = timestamp()


class SecurityEvent(Base):
    __tablename__ = "security_events"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("security_tenants.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="0")
    body: Mapped[dict] = mapped_column(JSONB)
    previous_hash: Mapped[str] = mapped_column(String(64), server_default="")
    event_hash: Mapped[str] = mapped_column(String(64), server_default="")
