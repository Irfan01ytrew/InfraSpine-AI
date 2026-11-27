"""Add owned scheduled folder sync, bounded diff history and in-app preferences.

Revision ID: 0040_folder_sync_monitor
Revises: 0039_policy_process_impact
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0040_folder_sync_monitor"
down_revision = "0039_policy_process_impact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "folder_sync_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", sa.String(24), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="RESTRICT")),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="ck_folder_sync_sources_name_length_valid"),
        sa.CheckConstraint("length(relative_path) BETWEEN 1 AND 1024", name="ck_folder_sync_sources_relative_path_length_valid"),
        sa.CheckConstraint("visibility IN ('private', 'shared', 'restricted')", name="ck_folder_sync_sources_visibility_valid"),
        sa.CheckConstraint("(visibility = 'restricted') = (department_group_id IS NOT NULL)", name="ck_folder_sync_sources_department_scope_valid"),
        sa.CheckConstraint("interval_minutes BETWEEN 5 AND 10080", name="ck_folder_sync_sources_interval_valid"),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_folder_sync_sources_failures_nonnegative"),
    )
    op.create_index("ix_folder_sync_sources_due", "folder_sync_sources", ["enabled", "next_sync_at", "locked_until"])

    op.create_table(
        "folder_sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("folder_sync_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('running', 'completed', 'partial', 'failed')", name="ck_folder_sync_runs_status_valid"),
        sa.CheckConstraint("attempt >= 1", name="ck_folder_sync_runs_attempt_valid"),
        sa.CheckConstraint("scanned >= 0 AND created >= 0 AND updated >= 0 AND unchanged >= 0 AND removed >= 0 AND duplicates >= 0 AND failed >= 0", name="ck_folder_sync_runs_counts_nonnegative"),
    )
    op.create_index("ix_folder_sync_runs_source_started", "folder_sync_runs", ["source_id", "started_at"])

    op.add_column("folder_sync_sources", sa.Column("locked_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_folder_sync_sources_locked_run_id_folder_sync_runs", "folder_sync_sources", "folder_sync_runs",
        ["locked_run_id"], ["id"], ondelete="SET NULL",
    )

    op.create_table(
        "folder_sync_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("change_alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "folder_sync_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("folder_sync_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "recipient_user_id", name="uq_folder_sync_notification_recipient"),
    )
    op.create_index("ix_folder_sync_notifications_recipient_created", "folder_sync_notifications", ["recipient_user_id", "created_at"])

    op.add_column("documents", sa.Column("folder_sync_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_documents_folder_sync_run_id_folder_sync_runs", "documents", "folder_sync_runs",
        ["folder_sync_run_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_documents_folder_sync_run_id", "documents", ["folder_sync_run_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_folder_sync_run_id", table_name="documents")
    op.drop_constraint("fk_documents_folder_sync_run_id_folder_sync_runs", "documents", type_="foreignkey")
    op.drop_column("documents", "folder_sync_run_id")
    op.drop_index("ix_folder_sync_notifications_recipient_created", table_name="folder_sync_notifications")
    op.drop_table("folder_sync_notifications")
    op.drop_table("folder_sync_preferences")
    op.drop_constraint("fk_folder_sync_sources_locked_run_id_folder_sync_runs", "folder_sync_sources", type_="foreignkey")
    op.drop_column("folder_sync_sources", "locked_run_id")
    op.drop_index("ix_folder_sync_runs_source_started", table_name="folder_sync_runs")
    op.drop_table("folder_sync_runs")
    op.drop_index("ix_folder_sync_sources_due", table_name="folder_sync_sources")
    op.drop_table("folder_sync_sources")
