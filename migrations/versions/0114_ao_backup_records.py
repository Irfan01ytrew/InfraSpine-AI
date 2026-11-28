"""Step AO slice 2: backup receipts and restore-drill records.

Revision ID: 0114_ao_backup_records
Revises: 0103_unified_studio_governance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114_ao_backup_records"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ao_backup_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("database", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=sa.text("''")),
        sa.Column("catalog_entries", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tables", sa.BigInteger(), nullable=True),
        sa.Column("rows_total", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("drill_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ao_backup_receipts")),
    )
    op.create_index(
        "ix_ao_backup_receipts_database_created", "ao_backup_receipts", ["database", "created_at"]
    )
    op.create_index("uq_ao_backup_receipts_path", "ao_backup_receipts", ["path"], unique=True)
    op.create_table(
        "ao_restore_drills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_database", sa.String(length=128), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("tables_expected", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tables_restored", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_checked", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_matched", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("sha256_match", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["ao_backup_receipts.id"], name=op.f("fk_ao_restore_drills_receipt_id_ao_backup_receipts"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ao_restore_drills")),
    )
    op.create_index(
        "ix_ao_restore_drills_receipt_created", "ao_restore_drills", ["receipt_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("ao_restore_drills")
    op.drop_table("ao_backup_receipts")
