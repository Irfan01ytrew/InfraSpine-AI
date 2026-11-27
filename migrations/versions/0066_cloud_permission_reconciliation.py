"""Persist a private cursor for bounded cloud permission reconciliation.

Revision ID: 0066_cloud_permission_reconciliation
Revises: 0065_workflow_personal_task_action
"""

from alembic import op
import sqlalchemy as sa

revision = "0066_cloud_permission_reconciliation"
down_revision = "0065_workflow_personal_task_action"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cloud_connector_configs",
        sa.Column("permission_reconcile_cursor", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    active_cursors = bind.execute(
        sa.text(
            "SELECT count(*) FROM cloud_connector_configs "
            "WHERE permission_reconcile_cursor IS NOT NULL"
        )
    ).scalar_one()
    if active_cursors:
        raise RuntimeError(
            "Cannot remove cloud permission reconciliation while a provider scan cursor is active."
        )
    op.drop_column("cloud_connector_configs", "permission_reconcile_cursor")
