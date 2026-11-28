"""Step W slice 5: scheduled self-test suite run history.

Revision ID: 0131_self_healing_test_runs
Revises: 0105_self_healing_escalations

NOTE (merge time): revision 0106 is also claimed on other agent branches
(K, M, G, L). Renumber at merge.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0131_self_healing_test_runs"
down_revision: str | None = "0130_self_healing_escalations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "self_healing_test_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("suite", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_names", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "suite IN ('focused', 'full')",
            name="self_healing_test_run_suite_valid",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'error')",
            name="self_healing_test_run_status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_self_healing_test_runs_suite_started",
        "self_healing_test_runs", ["suite", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_self_healing_test_runs_suite_started", table_name="self_healing_test_runs")
    op.drop_table("self_healing_test_runs")
