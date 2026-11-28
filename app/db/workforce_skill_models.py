"""Step J (AI Workforce OS), slice 2: persistence for the skill registry.

Two tables:

* ``workforce_skills`` — the skill catalog: one row per normalized skill
  name. Names are unique after normalization (see
  ``app.workforce.skills.validate_skill_name``), so "Invoice Triage" and
  "invoice triage" can never become two skills.
* ``workforce_agent_skills`` — one row per (agent kind, agent id, skill):
  the skill is *attached* to the agent, and separately *verified* for it.
  Verification is a fact (verified_by / verified_at / expires_at), never a
  permission: a verified skill does not grant the agent anything, and an
  unverified or expired skill never counts as a qualification for
  assignment (slice 1 only ever receives the current skill set).

Agent identity follows the AK control kinds (``studio`` / ``governance`` /
``profile``). A skill row does not FK to the island tables — the islands
are three separate tables and a skill must survive an agent being
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

WORKFORCE_AGENT_KINDS = ("studio", "governance", "profile")


class WorkforceSkill(Base):
    """One skill in the org-wide catalog."""

    __tablename__ = "workforce_skills"
    __table_args__ = (
        CheckConstraint(
            "char_length(name) >= 2 AND char_length(name) <= 60",
            name="ck_workforce_skill_name_length",
        ),
        UniqueConstraint("name", name="uq_workforce_skills_name"),
        Index("ix_workforce_skills_category", "category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    category: Mapped[str | None] = mapped_column(String(60))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkforceAgentSkill(Base):
    """One skill attached to one workforce agent, with its verification.

    ``verified_at`` / ``verified_by_user_id`` / ``expires_at`` are all set
    together by verification and all cleared together by revocation — a
    half-verified row cannot exist (see the check constraint). Status is
    derived, never stored: verified + unexpired = current, anything else
    does not count for assignment.
    """

    __tablename__ = "workforce_agent_skills"
    __table_args__ = (
        CheckConstraint(
            "agent_kind IN ('studio', 'governance', 'profile')",
            name="ck_workforce_agent_skill_kind_valid",
        ),
        CheckConstraint(
            "(verified_at IS NULL AND verified_by_user_id IS NULL AND expires_at IS NULL) OR "
            "(verified_at IS NOT NULL AND verified_by_user_id IS NOT NULL)",
            name="ck_workforce_agent_skill_verification_atomic",
        ),
        CheckConstraint(
            "expires_at IS NULL OR verified_at IS NULL OR expires_at > verified_at",
            name="ck_workforce_agent_skill_expiry_after_verification",
        ),
        UniqueConstraint(
            "agent_kind", "agent_id", "skill_id",
            name="uq_workforce_agent_skills_agent_skill",
        ),
        Index("ix_workforce_agent_skills_agent", "agent_kind", "agent_id"),
        Index("ix_workforce_agent_skills_skill", "skill_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workforce_skills.id", ondelete="CASCADE"), nullable=False)
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
