"""Step J (AI Workforce OS), slice 3: persistence for the org registry.

Two tables:

* ``workforce_registry_agents`` — one row per workforce agent that has
  had a lifecycle change: the org-level lifecycle state
  (``active`` / ``paused`` / ``retired``) plus a denormalized snapshot
  of the accountable owner as of the last lifecycle write. Agents that
  have never had a lifecycle change have no row and read as ``active``
  (subject to the island status and the owner-revocation derivation —
  see ``app.workforce.registry``). The row never reassigns ownership:
  the owner snapshot is refreshed only from the island agent itself on
  a lifecycle write, and the revocation hook never writes a new owner.
* ``workforce_lifecycle_events`` — append-only history: every lifecycle
  change (who, when, from -> to, why), including pauses caused by an
  owner revocation. There is no update-in-place and no delete here.

A registry row does not FK to the island tables — the islands are three
separate tables and a registry entry must survive an agent being
re-created — so the service resolves the agent (and its accountable
owner) from the island tables at write time and fails closed when the
agent does not exist.
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

WORKFORCE_LIFECYCLE_STATES = ("active", "paused", "retired")


class WorkforceRegistryAgent(Base):
    """The org-level lifecycle entry for one workforce agent."""

    __tablename__ = "workforce_registry_agents"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('active', 'paused', 'retired')",
            name="ck_workforce_registry_agent_state_valid",
        ),
        CheckConstraint(
            "agent_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_registry_agent_kind_valid",
        ),
        UniqueConstraint(
            "agent_kind", "agent_id",
            name="uq_workforce_registry_agents_agent",
        ),
        Index("ix_workforce_registry_agents_owner", "owner_user_id"),
        Index("ix_workforce_registry_agents_state", "lifecycle_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Accountable owner as of the last lifecycle write (live island owner
    # is the source of truth; the view reports both and notes a drift).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    # Department manager group for profile agents (department-owned).
    manager_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    state_reason: Mapped[str | None] = mapped_column(String(500))
    state_changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkforceLifecycleEvent(Base):
    """One lifecycle change, append-only. No update, no delete."""

    __tablename__ = "workforce_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "agent_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_lifecycle_event_kind_valid",
        ),
        CheckConstraint(
            "to_state IN ('active', 'paused', 'retired')",
            name="ck_workforce_lifecycle_event_to_state_valid",
        ),
        Index("ix_workforce_lifecycle_events_agent", "agent_kind", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    # Accountable owner at the time of the event (plain data, kept even
    # if the user row is later removed).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
