"""Heartbeats of document worker processes (status reporting only; jobs stay on the documents queue).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_workers",
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("active_jobs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('worker', 'embedded')", name=op.f("ck_ingestion_workers_mode_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_workers")),
    )
    op.create_index("ix_ingestion_workers_heartbeat", "ingestion_workers", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_workers_heartbeat", table_name="ingestion_workers")
    op.drop_table("ingestion_workers")
