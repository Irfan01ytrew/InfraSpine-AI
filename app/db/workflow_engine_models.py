"""Module 05 persistence. Scope is an existing department in the tenant database."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def scope():
    return mapped_column(ForeignKey('access_groups.id', ondelete='RESTRICT'), nullable=False)


def timestamp():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Workflow(Base):
    __tablename__ = 'wf_definitions'
    __table_args__ = (
        UniqueConstraint('scope_id', 'id'), Index('ix_wf_definitions_scope', 'scope_id', 'updated_at'),
        Index('uq_wf_definitions_origin_ref', 'scope_id', 'origin', 'origin_ref', unique=True,
              postgresql_where=text('origin_ref IS NOT NULL')),
        CheckConstraint("origin IN ('workflow','automation','system','studio','governance','founder_voice')", name='origin_valid'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(2000), default='')
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
    active_version: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Step E: one runtime. `origin` says which legacy path a definition came from (automation mirror,
    # system template); `origin_ref` is the legacy key. System templates are code-defined and pinned.
    origin: Mapped[str] = mapped_column(String(32), default='workflow', server_default=text("'workflow'"))
    origin_ref: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = timestamp()
    updated_at: Mapped[datetime] = timestamp()


class WorkflowVersion(Base):
    __tablename__ = 'wf_versions'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'workflow_id'], ['wf_definitions.scope_id', 'wf_definitions.id'], ondelete='RESTRICT'),
        UniqueConstraint('workflow_id', 'number'), UniqueConstraint('scope_id', 'id'),
        CheckConstraint("status IN ('draft', 'review', 'published', 'rejected')", name='status_valid'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    number: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='draft')
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = timestamp()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowRun(Base):
    __tablename__ = 'wf_runs'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'workflow_id'], ['wf_definitions.scope_id', 'wf_definitions.id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['scope_id', 'version_id'], ['wf_versions.scope_id', 'wf_versions.id'], ondelete='RESTRICT'),
        UniqueConstraint('scope_id', 'id'), UniqueConstraint('workflow_id', 'idempotency_key'),
        CheckConstraint("status IN ('queued','running','paused','completed','failed','cancelled','compensating','compensated')", name='status_valid'),
        CheckConstraint("phase IN ('plan','execute','verify','recover','done')", name='phase_valid'),
        Index('ix_wf_runs_due', 'status', 'wake_at'), Index('ix_wf_runs_scope_created', 'scope_id', 'created_at'),
        Index('ix_wf_runs_origin', 'origin', 'origin_ref'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('wf_runs.id', ondelete='RESTRICT'))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    input_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)  # AES-GCM envelope, AAD includes scope and row
    status: Mapped[str] = mapped_column(String(20), default='queued')
    simulation: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    event_hash: Mapped[str] = mapped_column(String(64), default='0' * 64)
    wake_at: Mapped[datetime] = timestamp()
    created_at: Mapped[datetime] = timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Step E: plan -> execute -> verify -> recover lifecycle, the recorded plan, origin and run timeout.
    phase: Mapped[str] = mapped_column(String(16), default='plan', server_default=text("'plan'"))
    plan: Mapped[list | None] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(32), default='workflow', server_default=text("'workflow'"))
    origin_ref: Mapped[str | None] = mapped_column(String(160))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason: Mapped[str | None] = mapped_column(String(120))
    last_verified_node: Mapped[str | None] = mapped_column(String(64))


class WorkflowStep(Base):
    __tablename__ = 'wf_steps'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'run_id'], ['wf_runs.scope_id', 'wf_runs.id'], ondelete='RESTRICT'),
        UniqueConstraint('run_id', 'node_id'), UniqueConstraint('scope_id', 'id'),
        CheckConstraint("status IN ('blocked','ready','running','human','approval','timer','event','child','dispatched','action','waiting','completed','skipped','failed','cancelled','compensated')", name='status_valid'),
        CheckConstraint("phase IN ('plan','execute','verify','recover','done')", name='phase_valid'),
        Index('ix_wf_steps_inbox', 'scope_id', 'status', 'assignee_id'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='blocked')
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = timestamp()
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_digest: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('wf_runs.id', ondelete='RESTRICT'))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Step E: permissioned action steps run as the acting principal; progress is checkpointed and a step is
    # only completed after its handler read back the target state (verified_at / verification).
    phase: Mapped[str] = mapped_column(String(16), default='plan', server_default=text("'plan'"))
    acting_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    acting_auth_version: Mapped[int | None] = mapped_column(Integer)
    checkpoint: Mapped[dict | None] = mapped_column(JSONB)
    recoveries: Mapped[int] = mapped_column(Integer, default=0, server_default=text('0'))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification: Mapped[dict | None] = mapped_column(JSONB)


class WorkflowDecision(Base):
    __tablename__ = 'wf_decisions'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'version_id'], ['wf_versions.scope_id', 'wf_versions.id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['scope_id', 'step_id'], ['wf_steps.scope_id', 'wf_steps.id'], ondelete='RESTRICT'),
        UniqueConstraint('version_id', 'authority_id'), UniqueConstraint('step_id', 'action_digest', 'authority_id'),
        CheckConstraint('(version_id IS NULL) <> (step_id IS NULL)', name='one_subject'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    authority_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    approved: Mapped[bool] = mapped_column(Boolean)
    action_digest: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = timestamp()


class WorkflowEvent(Base):
    __tablename__ = 'wf_events'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'run_id'], ['wf_runs.scope_id', 'wf_runs.id'], ondelete='RESTRICT'),
        UniqueConstraint('run_id', 'sequence'), Index('ix_wf_events_scope_run', 'scope_id', 'run_id', 'sequence'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(80))
    node_id: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    detail: Mapped[dict] = mapped_column(JSONB)
    previous_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = timestamp()


class WorkflowDispatch(Base):
    __tablename__ = 'wf_dispatches'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'step_id'], ['wf_steps.scope_id', 'wf_steps.id'], ondelete='RESTRICT'),
        UniqueConstraint('step_id', 'compensation'),
        CheckConstraint("status IN ('pending','running','completed','failed','unknown','cancelled')", name='status_valid'),
        Index('ix_wf_dispatch_due', 'status', 'available_at'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    adapter: Mapped[str] = mapped_column(String(80))
    compensation: Mapped[bool] = mapped_column(Boolean, default=False)
    action_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = timestamp()
    failure_code: Mapped[str | None] = mapped_column(String(80))


class WorkflowNotice(Base):
    __tablename__ = 'wf_notices'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'step_id'], ['wf_steps.scope_id', 'wf_steps.id'], ondelete='RESTRICT'),
        UniqueConstraint('step_id', 'recipient_id', 'kind'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    kind: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = timestamp()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowSignal(Base):
    __tablename__ = 'wf_signals'
    __table_args__ = (
        ForeignKeyConstraint(['scope_id', 'run_id'], ['wf_runs.scope_id', 'wf_runs.id'], ondelete='RESTRICT'),
        UniqueConstraint('run_id', 'idempotency_key'),
    )
    id: Mapped[uuid.UUID] = uid()
    scope_id: Mapped[uuid.UUID] = scope()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('wf_steps.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = timestamp()
