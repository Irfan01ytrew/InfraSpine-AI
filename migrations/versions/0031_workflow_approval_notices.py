"""Notify authorized reviewers when a workflow run awaits approval.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"), "workflow_notifications", type_="check"
    )
    op.create_check_constraint(
        "notification_type_valid",
        "workflow_notifications",
        "notification_type IN ('run_approved', 'run_rejected', 'task_assigned', 'approval_required')",
    )
    op.drop_constraint(op.f("ck_workflow_notifications_task_reference_valid"), "workflow_notifications", type_="check")
    op.create_check_constraint(
        "task_reference_valid",
        "workflow_notifications",
        "(notification_type = 'task_assigned' AND task_id IS NOT NULL) OR "
        "(notification_type IN ('run_approved', 'run_rejected', 'approval_required') AND task_id IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM workflow_notifications WHERE notification_type = 'approval_required') THEN
                RAISE EXCEPTION 'resolve approval_required notifications before downgrading migration 0031';
            END IF;
        END $$;"""
    )
    op.drop_constraint(op.f("ck_workflow_notifications_task_reference_valid"), "workflow_notifications", type_="check")
    op.create_check_constraint(
        "task_reference_valid",
        "workflow_notifications",
        "(notification_type = 'task_assigned' AND task_id IS NOT NULL) OR "
        "(notification_type IN ('run_approved', 'run_rejected') AND task_id IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"), "workflow_notifications", type_="check"
    )
    op.create_check_constraint(
        "notification_type_valid",
        "workflow_notifications",
        "notification_type IN ('run_approved', 'run_rejected', 'task_assigned')",
    )
