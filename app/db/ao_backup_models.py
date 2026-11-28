"""Step AO backup records: receipts for every backup run and verdicts for restore drills.

A receipt is written for every backup attempt — verified, skipped, or
failed — so "do we have a backup" is answered from the database, never
from a file listing. A restore drill row is written for every drill;
only a drill that passes marks its receipt ``drill_verified``.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: A backup run that produced no backup file because the source database
#: had no catalog entries (an empty database). The run is recorded, not
#: hidden — but the database is NOT counted as backed up.
STATUS_SKIPPED = "skipped"
#: pg_dump ran and ``pg_restore --list`` could read the catalog.
STATUS_VERIFIED = "verified"
#: The backup attempt failed (pg_dump error, verification error, ...).
STATUS_FAILED = "failed"

RECEIPT_STATUSES = (STATUS_SKIPPED, STATUS_VERIFIED, STATUS_FAILED)


class BackupReceipt(Base):
    """One backup attempt against one database."""

    __tablename__ = "ao_backup_receipts"
    __table_args__ = (
        Index("ix_ao_backup_receipts_database_created", "database", "created_at"),
        Index("uq_ao_backup_receipts_path", "path", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    database: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    catalog_entries: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: User tables present when the backup ran (NULL when unknown, e.g. a failed run).
    tables: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Total rows across user tables when the backup ran (NULL when unknown).
    rows_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: True only after a restore drill passed for this receipt.
    drill_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Human-readable reason for a skipped or failed run; NULL for verified runs.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RestoreDrill(Base):
    """One restore drill: a backup restored into an empty throwaway database and compared."""

    __tablename__ = "ao_restore_drills"
    __table_args__ = (
        Index("ix_ao_restore_drills_receipt_created", "receipt_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ao_backup_receipts.id", ondelete="CASCADE"), nullable=True
    )
    target_database: Mapped[str] = mapped_column(String(128), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failures: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    tables_expected: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tables_restored: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rows_checked: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rows_matched: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sha256_match: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
