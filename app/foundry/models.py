"""Step AM (Self-Evolving Software Foundry), slice 6 — registry persistence.

Three tables, one per registry concern:

* `foundry_apps` — one row per generated app per platform scope. The slug
  is unique inside its scope; the app name is display text only.
* `foundry_builds` — one row per build (spec version). Carries the spec
  fingerprint and build fingerprint slices 1–2 produce, the slice-3 gate
  and health, the slice-4 deploy state + approval, and the spec + test
  summary as JSONB for the detail view. A build fingerprint is recorded
  once (unique); builds are append-only — a new version is a new row.
* `foundry_feedback` — one row per feedback item, attached to one build
  fingerprint. The free-text note is stored for a human to read; the
  structured change actions are stored alongside it.

No secrets are stored here: specs, feedback and generated code are
rejected by slice 1 if they contain anything that looks like a secret.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

__all__ = ["FoundryApp", "FoundryBuild", "FoundryFeedback"]


class FoundryApp(Base):
    __tablename__ = "foundry_apps"
    __table_args__ = (
        UniqueConstraint("scope_id", "slug", name="uq_foundry_apps_scope_slug"),
        Index("ix_foundry_apps_scope_id", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FoundryBuild(Base):
    __tablename__ = "foundry_builds"
    __table_args__ = (
        UniqueConstraint("build_fingerprint", name="uq_foundry_builds_fingerprint"),
        CheckConstraint("gate IN ('blocked', 'preview_ready')", name="foundry_build_gate_valid"),
        CheckConstraint("health IN ('pending', 'passed', 'failed')", name="foundry_build_health_valid"),
        CheckConstraint(
            "deploy_state IN ('none', 'requested', 'approved', 'deploying', 'deployed', 'rolled_back', 'failed')",
            name="foundry_build_deploy_state_valid"),
        CheckConstraint("spec_version > 0", name="foundry_build_version_positive"),
        Index("ix_foundry_builds_app_id", "app_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("foundry_apps.id", ondelete="CASCADE"), nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    build_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    gate: Mapped[str] = mapped_column(String(20), nullable=False, default="blocked")
    preview_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    preview_expires_in_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    health: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    tests_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deploy_state: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    deploy_target: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    test_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FoundryFeedback(Base):
    __tablename__ = "foundry_feedback"
    __table_args__ = (
        CheckConstraint("kind IN ('bug', 'change_request', 'praise', 'question')",
                        name="foundry_feedback_kind_valid"),
        Index("ix_foundry_feedback_app_id", "app_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("foundry_apps.id", ondelete="CASCADE"), nullable=False)
    build_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    changes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
