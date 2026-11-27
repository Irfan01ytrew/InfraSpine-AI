"""Persist encrypted cloud connector configuration and sync state.

Revision ID: 0061_persist_cloud_connectors
Revises: 0060_agent_profile_run_history
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0061_persist_cloud_connectors"
down_revision = "0060_agent_profile_run_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_connector_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("connector_type", sa.String(length=24), nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="restricted", nullable=False),
        sa.Column(
            "department_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sync_interval_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sync_in_progress", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=16), nullable=True),
        sa.Column("last_sync_error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_cursor", sa.Text(), nullable=True),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("credential_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("credential_key_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "connector_type IN ('google_drive', 'onedrive', 'sharepoint')",
            name="ck_cloud_connector_configs_connector_type_valid",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'shared', 'restricted')",
            name="ck_cloud_connector_configs_visibility_valid",
        ),
        sa.CheckConstraint(
            "sync_interval_minutes BETWEEN 5 AND 10080",
            name="ck_cloud_connector_configs_sync_interval_valid",
        ),
        sa.CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status IN ('success', 'partial', 'failed')",
            name="ck_cloud_connector_configs_last_sync_status_valid",
        ),
        sa.CheckConstraint("last_sync_error_count >= 0", name="ck_cloud_connector_configs_error_count_nonnegative"),
    )
    op.create_index(
        "ix_cloud_connector_configs_active_created",
        "cloud_connector_configs",
        ["is_active", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cloud_connector_configs_active_created", table_name="cloud_connector_configs")
    op.drop_table("cloud_connector_configs")
