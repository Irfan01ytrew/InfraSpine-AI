"""What-if rehearsal records (Step AL slice 5).

A rehearsal record stores who practised a simulated recovery plan,
when, which steps, and what was found. It is data only: a record
never executes anything (see ``app.whatif.rehearsal`` — every record
is a dry run with no side effects by construction).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WhatIfRehearsal(Base):
    __tablename__ = "whatif_rehearsals"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress','completed')", name="whatif_rehearsal_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_graph_tenants.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    scenario_kind: Mapped[str] = mapped_column(String(40))
    template_id: Mapped[str | None] = mapped_column(String(64))
    seed_ids: Mapped[list] = mapped_column(JSONB, default=list)
    seed_names: Mapped[list] = mapped_column(JSONB, default=list)
    horizon_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    findings: Mapped[list] = mapped_column(JSONB, default=list)
    rehearsed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rehearsed_by_name: Mapped[str | None] = mapped_column(String(180))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    completed_by_name: Mapped[str | None] = mapped_column(String(180))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
