"""Persisted twin health alerts (Step K v2 slice 3).

One row per fired watch alert, per graph tenant. Alerts are
advisory only — a row records that a health rule fired for one twin
version; it never approves, dispatches, grants access, or pays
anything. The dedupe fingerprint is ``(rule, tenant, twin
version)`` (see :func:`app.digital_twin.health_watch.watch_fingerprint`),
so the same rule firing for the same twin version is stored once;
a new twin version may fire the same rule again (subject to the
rule's cooldown, enforced by the service before inserting).

Alert detail carries score points and counts only — no money
amounts, no entity names — so alerts are safe to show to any tenant
member. Rows are never edited; old rows only leave through the
retention rule (newest 100 alerts per tenant, nothing older than 90
days, enforced by the service, not the DB). Dropping this table
loses alert history, not the twin: alerts are re-derived by
re-evaluating the watch over the stored versions.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TwinHealthAlert(Base):
    __tablename__ = "twin_health_alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_health_alerts_tenant_fingerprint"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_health_alerts_fingerprint_hex"),
        CheckConstraint("health_score >= 0 AND health_score <= 100", name="ck_twin_health_alerts_score_range"),
        Index("ix_twin_health_alerts_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    health_score: Mapped[int] = mapped_column(Integer, nullable=False)
    drift_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trust: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["TwinHealthAlert"]
