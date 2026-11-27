"""Tenant-scoped continuity records; references cannot cross a company boundary."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Tenant(Base):
    __tablename__ = "bc_tenants"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    event_head: Mapped[str] = mapped_column(String(64), default="0" * 64)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = "bc_members"
    __table_args__ = (CheckConstraint("role IN ('owner','approver','planner','executor','viewer')", name="role"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    departments: Mapped[list] = mapped_column(JSONB, default=list)
    all_departments: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Node(Base):
    """Continuity projection of an authorized source, not a replacement company directory."""
    __tablename__ = "bc_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "external_key", name="uq_bc_nodes_scope_key"),
        UniqueConstraint("access_group_id"),
        ForeignKeyConstraint(["tenant_id", "department_id"], ["bc_nodes.tenant_id", "bc_nodes.id"]),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    access_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    external_key: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(180))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Link(Base):
    __tablename__ = "bc_links"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["bc_nodes.tenant_id", "bc_nodes.id"], name="fk_bc_link_source"),
        ForeignKeyConstraint(["tenant_id", "target_id"], ["bc_nodes.tenant_id", "bc_nodes.id"], name="fk_bc_link_target"),
        UniqueConstraint("tenant_id", "source_id", "target_id", "kind"),
        CheckConstraint("source_id <> target_id", name="distinct_endpoints"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSONB)


class Assessment(Base):
    __tablename__ = "bc_assessments"
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "node_id"], ["bc_nodes.tenant_id", "bc_nodes.id"]),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID)
    data: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Scenario(Base):
    __tablename__ = "bc_scenarios"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "source_key", name="uq_bc_scenarios_scope_key"))
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(40))
    source_key: Mapped[str | None] = mapped_column(String(180))
    data: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "bc_plans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "family_id", "version", name="uq_bc_plans_scope_key"),
        ForeignKeyConstraint(["tenant_id", "department_id"], ["bc_nodes.tenant_id", "bc_nodes.id"]),
        CheckConstraint("status IN ('pending','approved','rejected')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    data: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    approval_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "bc_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "idempotency_key", name="uq_bc_runs_scope_key"),
        ForeignKeyConstraint(["tenant_id", "plan_id"], ["bc_plans.tenant_id", "bc_plans.id"]),
        ForeignKeyConstraint(["tenant_id", "scenario_id"], ["bc_scenarios.tenant_id", "bc_scenarios.id"]),
        CheckConstraint("status IN ('pending','active','suspended','rejected','closed')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    mode: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    approval_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disruption_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "bc_tasks"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["bc_runs.tenant_id", "bc_runs.id"]),
        UniqueConstraint("tenant_id", "run_id", "step_key"),
        UniqueConstraint("workflow_run_id"),
        CheckConstraint("status IN ('pending','dispatched','evidence_submitted','verified')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID)
    step_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("automation_runs.id"))
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    """Append-only, hash-chained journal. A DB owner can still replace the whole chain."""
    __tablename__ = "bc_events"
    __table_args__ = (UniqueConstraint("tenant_id", "sequence"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID)
    data: Mapped[dict] = mapped_column(JSONB)
    previous_hash: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GrantRequest(Base):
    __tablename__ = "bc_grant_requests"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bc_tenants.id"), index=True)
    data: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
