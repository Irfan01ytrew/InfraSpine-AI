"""Persisted forecast records (Step L slice 3).

One row per scan forecast: the forecast itself plus the twin snapshot
it was computed from, so a later scan can score how stale the older
record has become. Records are advisory — they are written only by
the scan path (``record_scan``), never by a GET — and the newest
record per (tenant, root, horizon) is the current forecast; older
records with the same key are superseded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ForecastRecord(Base):
    """A persisted predictive-continuity forecast for one root entity."""

    __tablename__ = "forecast_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "root_id", "horizon_days", "fingerprint",
            name="uq_forecast_records_tenant_root_horizon_fp",
        ),
        Index("ix_forecast_records_root_created", "tenant_id", "root_id",
              "horizon_days", "created_at"),
        Index("ix_forecast_records_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("horizon_days BETWEEN 1 AND 365",
                        name="ck_forecast_records_horizon_valid"),
        CheckConstraint("drift_score IS NULL OR (drift_score >= 0 AND drift_score <= 100)",
                        name="ck_forecast_records_drift_valid"),
        CheckConstraint("jsonb_typeof(forecast) = 'object'",
                        name="ck_forecast_records_forecast_is_object"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'",
                        name="ck_forecast_records_snapshot_is_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_graph_tenants.id", ondelete="CASCADE"),
        nullable=False)
    root_id: Mapped[str] = mapped_column(String(200), nullable=False)
    root_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drift_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")
