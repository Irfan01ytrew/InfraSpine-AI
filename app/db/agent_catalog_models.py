"""Reviewed, data-only internal agent templates with immutable versions."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class InternalAgentCatalogTemplate(Base):
    """A revocable catalog identity; versions and review records remain for history."""

    __tablename__ = "internal_agent_catalog_templates"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_valid"),
        CheckConstraint("latest_version > 0", name="latest_version_positive"),
        CheckConstraint(
            "published_version IS NULL OR published_version BETWEEN 1 AND latest_version",
            name="published_version_valid",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL AND revoke_rationale IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL AND revoke_rationale IS NOT NULL AND "
            "char_length(btrim(revoke_rationale)) BETWEEN 3 AND 2000)",
            name="revocation_complete",
        ),
        UniqueConstraint("slug", name="uq_internal_agent_catalog_templates_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    published_version: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    revoke_rationale: Mapped[str | None] = mapped_column(String(2000))


class InternalAgentCatalogVersion(Base):
    """Immutable configuration, with only review lifecycle fields mutable."""

    __tablename__ = "internal_agent_catalog_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_internal_agent_catalog_versions_number"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 120", name="name_valid"),
        CheckConstraint("char_length(btrim(summary)) BETWEEN 1 AND 2000", name="summary_valid"),
        CheckConstraint(
            "agent_type IN ('knowledge_research', 'market_research', 'company_analysis', 'employee_training')",
            name="agent_type_valid",
        ),
        CheckConstraint("knowledge_scope IN ('requester_acl', 'department_only')", name="knowledge_scope_valid"),
        CheckConstraint(
            "jsonb_typeof(tool_policy) = 'array' AND "
            "jsonb_array_length(tool_policy) BETWEEN 1 AND 4 AND "
            "tool_policy <@ '[\"knowledge_search\", \"public_web_research\", "
            "\"company_data_analysis\", \"training_plan\"]'::jsonb",
            name="tool_policy_valid",
        ),
        CheckConstraint("status IN ('draft', 'pending_review', 'approved', 'rejected', 'superseded')", name="status_valid"),
        CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND submitted_by_user_id IS NULL AND "
            "reviewed_at IS NULL AND reviewed_by_user_id IS NULL AND review_rationale IS NULL) OR "
            "(status = 'pending_review' AND submitted_at IS NOT NULL AND submitted_by_user_id IS NOT NULL AND "
            "reviewed_at IS NULL AND reviewed_by_user_id IS NULL AND review_rationale IS NULL) OR "
            "(status IN ('approved', 'rejected', 'superseded') AND submitted_at IS NOT NULL AND "
            "submitted_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by_user_id IS NOT NULL AND "
            "review_rationale IS NOT NULL AND char_length(btrim(review_rationale)) BETWEEN 3 AND 2000 AND "
            "reviewed_by_user_id <> author_user_id AND reviewed_by_user_id <> submitted_by_user_id AND "
            "reviewed_at >= submitted_at)",
            name="review_lifecycle_valid",
        ),
        Index("ix_internal_agent_catalog_versions_queue", "status", "created_at"),
        Index(
            "uq_internal_agent_catalog_versions_pending_template",
            "template_id",
            unique=True,
            postgresql_where=text("status = 'pending_review'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("internal_agent_catalog_templates.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    knowledge_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    tool_policy: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'draft'"))
    author_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_rationale: Mapped[str | None] = mapped_column(String(2000))


class InternalAgentCatalogGrant(Base):
    """An explicit publisher/reviewer grant. Revocation is retained, never overwritten."""

    __tablename__ = "internal_agent_catalog_grants"
    __table_args__ = (
        CheckConstraint("permission IN ('publish', 'review')", name="permission_valid"),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="revocation_complete",
        ),
        Index(
            "uq_internal_agent_catalog_grants_active",
            "user_id",
            "permission",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_internal_agent_catalog_grants_user", "user_id", "permission", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class InternalAgentCatalogEvent(Base):
    """Append-only history for submission, independent review, and immediate revocation."""

    __tablename__ = "internal_agent_catalog_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('version_created', 'version_submitted', 'version_approved', 'version_rejected', 'catalog_revoked')",
            name="event_type_valid",
        ),
        CheckConstraint(
            "(event_type IN ('version_approved', 'version_rejected', 'catalog_revoked') AND rationale IS NOT NULL AND "
            "char_length(btrim(rationale)) BETWEEN 3 AND 2000) OR "
            "(event_type IN ('version_created', 'version_submitted') AND rationale IS NULL)",
            name="rationale_valid",
        ),
        Index("ix_internal_agent_catalog_events_template", "template_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("internal_agent_catalog_templates.id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("internal_agent_catalog_versions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
