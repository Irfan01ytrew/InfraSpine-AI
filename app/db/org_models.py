"""Positions (job titles) and the history of who held which position and reported to whom.

A position describes what a person is called or does. It never grants software permissions: access comes
from the software role, custom roles and department membership, which stay separate.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

JOB_FAMILIES = (
    "executive", "management", "professional", "technical", "operations",
    "administration", "governance", "sales", "frontline", "external", "other",
)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("length(btrim(title)) BETWEEN 2 AND 120", name="title_valid"),
        CheckConstraint("job_family IN (" + ", ".join(f"'{f}'" for f in JOB_FAMILIES) + ")", name="job_family_valid"),
        CheckConstraint("level IS NULL OR level BETWEEN 1 AND 20", name="level_valid"),
        # One active position per title, case-insensitively; archived titles can be reused.
        Index("uq_positions_active_title", text("lower(title)"), unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    job_family: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'other'"))
    # Seniority band, 1 = most senior. Optional: many organizations do not use grades.
    level: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrgAssignmentHistory(Base):
    """One row per change of a person's position or manager; the current row has `ended_at` NULL."""

    __tablename__ = "org_assignment_history"
    __table_args__ = (Index("ix_org_assignment_history_user", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    # Title as it read when assigned, so a later rename does not rewrite history.
    position_title: Mapped[str | None] = mapped_column(String(120))
    reports_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reports_to_email: Mapped[str | None] = mapped_column(String(320))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
