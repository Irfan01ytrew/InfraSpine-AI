"""Step AL slice 5: rehearsal records table.

Stores who practised a simulated recovery plan, when, which steps,
and what was found. Records are data only — a rehearsal never
executes anything (see app.whatif.rehearsal).

Revision ID: 0110_whatif_rehearsals
Revises: 0109_knowledge_expiry_alerts

NOTE: this revision number collides with the Step AG branch's
0104_hr_leave_attendance, which does not exist on this branch. The
number must be reconciled at merge time (rebase onto the then-current
head); the table itself is independent.

Renumbered at integration time (linear chain in INTEGRATION-SNAPSHOT.md, map v4).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0110_whatif_rehearsals"
down_revision: str | None = "0109_knowledge_expiry_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatif_rehearsals",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("scenario_kind", sa.String(40), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=True),
        sa.Column("seed_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("seed_names", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("horizon_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="in_progress"),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("findings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rehearsed_by", postgresql.UUID(), nullable=False),
        sa.Column("rehearsed_by_name", sa.String(180), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", postgresql.UUID(), nullable=True),
        sa.Column("completed_by_name", sa.String(180), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('in_progress','completed')", name="whatif_rehearsal_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["company_graph_tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rehearsed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["completed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_whatif_rehearsals_tenant_id", "whatif_rehearsals", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_whatif_rehearsals_tenant_id", table_name="whatif_rehearsals")
    op.drop_table("whatif_rehearsals")
