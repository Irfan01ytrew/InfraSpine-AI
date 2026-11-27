"""Add a durable, leased queue for safe agent runs.

Revision ID: 0064_durable_agent_run_queue
Revises: 0063_cloud_source_acl_intersection
"""

from alembic import op
import sqlalchemy as sa

revision = "0064_durable_agent_run_queue"
down_revision = "0063_cloud_source_acl_intersection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_profile_runs",
        sa.Column("execution_mode", sa.String(length=16), server_default="sync", nullable=False),
    )
    op.add_column(
        "agent_profile_runs",
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("agent_profile_runs", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("agent_profile_runs", "started_at", nullable=True)

    # Revision 0060 passed already-prefixed names into metadata that uses the
    # ck_<table>_<constraint_name> naming convention, so installed schemas have
    # the doubled names below. Drop those exact legacy constraints, then create
    # the single-prefix names that the current ORM metadata expects.
    op.drop_constraint(
        op.f("ck_agent_profile_runs_ck_agent_profile_runs_status_valid"),
        "agent_profile_runs",
        type_="check",
    )
    op.create_check_constraint(
        "status_valid", "agent_profile_runs", "status IN ('queued', 'running', 'completed', 'failed')"
    )
    op.drop_constraint(
        op.f("ck_agent_profile_runs_ck_agent_profile_runs_attempts_valid"),
        "agent_profile_runs",
        type_="check",
    )
    op.create_check_constraint("attempts_valid", "agent_profile_runs", "attempts BETWEEN 0 AND 2")
    op.create_check_constraint(
        "execution_mode_valid", "agent_profile_runs", "execution_mode IN ('sync', 'queue')"
    )
    op.create_index(
        "ix_agent_profile_runs_queue_available",
        "agent_profile_runs",
        ["available_at", "requested_at"],
        postgresql_where=sa.text("status = 'queued' AND execution_mode = 'queue'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    queued_work = bind.execute(
        sa.text(
            "SELECT count(*) FROM agent_profile_runs "
            "WHERE status = 'queued' OR attempts = 0 OR started_at IS NULL"
        )
    ).scalar_one()
    if queued_work:
        raise RuntimeError("Cannot remove durable agent queue columns while queued or unstarted runs remain.")
    op.drop_index("ix_agent_profile_runs_queue_available", table_name="agent_profile_runs")
    op.drop_constraint("execution_mode_valid", "agent_profile_runs", type_="check")
    op.drop_constraint("attempts_valid", "agent_profile_runs", type_="check")
    op.drop_constraint("status_valid", "agent_profile_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_agent_profile_runs_ck_agent_profile_runs_status_valid"),
        "agent_profile_runs",
        "status IN ('running', 'completed', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_agent_profile_runs_ck_agent_profile_runs_attempts_valid"),
        "agent_profile_runs",
        "attempts BETWEEN 1 AND 2",
    )
    op.alter_column("agent_profile_runs", "started_at", nullable=False)
    op.drop_column("agent_profile_runs", "locked_until")
    op.drop_column("agent_profile_runs", "available_at")
    op.drop_column("agent_profile_runs", "execution_mode")
