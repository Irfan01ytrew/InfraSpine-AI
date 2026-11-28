"""Step W — Self-Healing persistence: probe snapshots and incident records.

Probes are append-only health snapshots (one row per component per collection).
Incidents are one row per incident key carrying the auto-remedy attempt history
(attempts + last attempt) so the detector's budget and cooldown survive restarts.
Neither table holds business data, secrets, or money: only component health and
remedy bookkeeping. Dropping them loses history, never business state.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SelfHealingProbe(Base):
    __tablename__ = "self_healing_probes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'degraded', 'down', 'unknown')",
            name="self_healing_probe_status_valid",
        ),
        Index("ix_self_healing_probes_component_recorded", "component", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    age_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    error_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SelfHealingIncident(Base):
    __tablename__ = "self_healing_incidents"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="self_healing_incident_severity_valid",
        ),
        CheckConstraint(
            "status IN ('open', 'remedied', 'escalated')",
            name="self_healing_incident_status_valid",
        ),
        Index("ix_self_healing_incidents_status_updated", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SelfHealingEscalation(Base):
    """Step W slice 4 — one owner-facing escalation per incident key.

    A row is the delivery record for the admin-UI channel: the scrubbed report
    that reached the owner, when it was delivered, and whether a human has
    acknowledged it. A fault that recurs after acknowledgement reopens the
    escalation (acknowledgement cleared) instead of silently reusing the old
    one. WhatsApp/email delivery does not exist yet (Step F / integrations);
    the channel column only ever holds ``admin_ui`` today — other channels are
    refused by the service, never faked.
    """

    __tablename__ = "self_healing_escalations"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="self_healing_escalation_severity_valid",
        ),
        Index("ix_self_healing_escalations_acknowledged", "acknowledged_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    channel: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'admin_ui'"))
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

class SelfHealingTestRun(Base):
    """Step W slice 5 — one scheduled self-test suite run.

    The run record is the history the auto-test loop keeps: which suite ran,
    how long it took, and what failed. A failing run is triaged into a
    ``self_test_failed`` incident (slice 1's classifier, via ``triage``) and
    escalated to the owner — a failing test never triggers a remedy.
    """

    __tablename__ = "self_healing_test_runs"
    __table_args__ = (
        CheckConstraint(
            "suite IN ('focused', 'full')",
            name="self_healing_test_run_suite_valid",
        ),
        CheckConstraint(
            "status IN ('passed', 'failed', 'error')",
            name="self_healing_test_run_status_valid",
        ),
        Index("ix_self_healing_test_runs_suite_started", "suite", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    suite: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    failure_names: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
