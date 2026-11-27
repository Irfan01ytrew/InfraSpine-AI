"""Add approval-gated bounded legacy-browser requests.

Revision ID: 0056_legacy_browser_approval
Revises: 0055_workflow_sla_trigger
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0056_legacy_browser_approval"
down_revision = "0055_workflow_sla_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_browser_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("requester_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approver_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("start_url", sa.String(2048), nullable=False),
        sa.Column("link_labels", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending_approval", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("failure_code", sa.String(60)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'running', 'completed', 'failed', 'rejected', 'cancelled')",
            name=op.f("ck_legacy_browser_runs_status_valid"),
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 2", name=op.f("ck_legacy_browser_runs_attempts_valid")),
    )
    op.create_index(
        "ix_legacy_browser_runs_requester_status",
        "legacy_browser_runs",
        ["requester_user_id", "status"],
    )
    op.create_index(
        "ix_legacy_browser_runs_status_created",
        "legacy_browser_runs",
        ["status", "requested_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM legacy_browser_runs)")):
        raise RuntimeError("Export or retain legacy-browser approval history before downgrading migration 0056.")
    op.drop_index("ix_legacy_browser_runs_status_created", table_name="legacy_browser_runs")
    op.drop_index("ix_legacy_browser_runs_requester_status", table_name="legacy_browser_runs")
    op.drop_table("legacy_browser_runs")
