"""Persisted twin snapshot versions (Step K v2 slice 2).

One row per recorded twin snapshot, per graph tenant. Versions are
append-only: a snapshot is validated and fingerprinted before it is
stored, the same fingerprint for the same tenant is never stored
twice, and rows are never edited — old rows only leave through the
retention rule (newest 30 versions, nothing older than 90 days).
The stored snapshot is derived data: the live snapshot builder can
rebuild it, so dropping this table loses history, not the twin
itself.

``snapshot`` is the full twin snapshot dict as built by the Step S
snapshot builder (entities + relationships + sources). ``drift_report``
is the slice-1 ``diff_snapshots`` report against the previous version
for the same tenant (``None`` for the first version recorded).
``source_freshness`` is the per-source freshness list from the
snapshot build (``[{"name", "kind", "status", ...}]``).

Money amounts inside ``snapshot`` / ``drift_report`` are exposure
totals only, never payment instructions — and the API only shows them
to the tenant owner (see ``app.digital_twin.versions``).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TwinSnapshotVersion(Base):
    __tablename__ = "twin_snapshot_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_twin_snapshot_versions_tenant_fingerprint"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_twin_snapshot_versions_fingerprint_hex"),
        Index("ix_twin_snapshot_versions_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Monotonic insertion order. ``created_at`` uses ``now()`` (the
    # transaction timestamp), so versions recorded in one transaction
    # tie on it — ``seq`` is the deterministic "latest" / retention
    # order. It is global, not per-tenant; per-tenant numbering is a
    # display concern for the UI slice, not the store.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(start=1, increment=1), nullable=False, unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    drift_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_freshness: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["TwinSnapshotVersion"]
