"""Step J (AI Workforce OS), slice 4: persistence for delegation policy.

Two tables:

* ``workforce_delegation_policies`` — one row per policy version: the
  versioned rule set (JSONB, see ``app.workforce.delegation``) plus the
  depth cap. Exactly one policy is active at a time; the service
  deactivates the previous active policy when a new one is created (in
  the same transaction). Versions are never reused and never edited in
  place — a change is a new version.
* ``workforce_delegation_audits`` — append-only: every delegation
  decision the policy engine made (allowed and denied), with the parent
  run, the child execution, the policy version and the reason. The child
  executes inline in the parent's Agent Studio graph on this branch, so
  ``child_run_id`` is the delegation execution id (a fresh UUID per
  delegation) and ``child_run_kind`` is ``'inline'``; when the unified
  engine takes over delegation, ``child_run_id`` becomes the
  ``wf_runs`` id and ``child_run_kind`` becomes ``'workflow'`` — the
  audit shape does not change. There is no update-in-place and no delete
  here.

A policy row does not FK to the island tables — the islands are three
separate tables — so the service resolves both agents at decision time
and fails closed when either does not exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CHILD_RUN_KIND_INLINE = "inline"
CHILD_RUN_KIND_WORKFLOW = "workflow"


class WorkforceDelegationPolicy(Base):
    """One versioned delegation policy (exactly one active at a time)."""

    __tablename__ = "workforce_delegation_policies"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_workforce_delegation_policy_version"),
        CheckConstraint(
            "max_depth BETWEEN 1 AND 5",
            name="ck_workforce_delegation_policy_depth",
        ),
        UniqueConstraint("version", name="uq_workforce_delegation_policies_version"),
        Index("ix_workforce_delegation_policies_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(nullable=False)
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    max_depth: Mapped[int] = mapped_column(nullable=False, server_default=text("2"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkforceDelegationAudit(Base):
    """One delegation decision, append-only. No update, no delete."""

    __tablename__ = "workforce_delegation_audits"
    __table_args__ = (
        CheckConstraint(
            "parent_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_delegation_audit_parent_kind",
        ),
        CheckConstraint(
            "child_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_delegation_audit_child_kind",
        ),
        CheckConstraint(
            "decision IN ('allowed', 'denied')",
            name="ck_workforce_delegation_audit_decision",
        ),
        CheckConstraint(
            "child_run_kind IN ('inline', 'workflow')",
            name="ck_workforce_delegation_audit_child_run_kind",
        ),
        Index("ix_workforce_delegation_audits_child",
              "child_kind", "child_agent_id", "created_at"),
        Index("ix_workforce_delegation_audits_parent_run", "parent_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    child_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    child_agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    child_run_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'inline'"))
    policy_version: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    action: Mapped[str] = mapped_column(String(160), nullable=False, server_default=text("''"))
    scope_id: Mapped[str] = mapped_column(String(160), nullable=False, server_default=text("''"))
    depth: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
