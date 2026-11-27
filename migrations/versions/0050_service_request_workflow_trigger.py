"""Support event-triggered, approval-gated workflows for department help requests.

Revision ID: 0050_service_request_workflow_trigger
Revises: 0049_partner_identity_boundary
"""

from alembic import op
import sqlalchemy as sa


revision = "0050_service_request_workflow_trigger"
down_revision = "0049_partner_identity_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_automation_workflows_ck_automation_workflows_trigger_valid"), "automation_workflows", type_="check")
    op.create_check_constraint(
        "trigger_valid",
        "automation_workflows",
        "trigger_type IN ('manual', 'interval', 'service_request')",
    )
    op.drop_constraint(op.f("ck_automation_workflows_ck_automation_workflows_trigger_a201"), "automation_workflows", type_="check")
    op.create_check_constraint(
        "trigger_interval_valid",
        "automation_workflows",
        "(trigger_type IN ('manual', 'service_request') AND interval_minutes IS NULL) OR "
        "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_event_workflows = bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM automation_workflows WHERE trigger_type = 'service_request')")
    )
    if has_event_workflows:
        raise RuntimeError("Disable/remove service-request-trigger workflows before downgrading migration 0050.")
    op.drop_constraint(op.f("ck_automation_workflows_trigger_valid"), "automation_workflows", type_="check")
    op.create_check_constraint(
        op.f("ck_automation_workflows_ck_automation_workflows_trigger_valid"), "automation_workflows", "trigger_type IN ('manual', 'interval')"
    )
    op.drop_constraint(op.f("ck_automation_workflows_trigger_interval_valid"), "automation_workflows", type_="check")
    op.create_check_constraint(
        op.f("ck_automation_workflows_ck_automation_workflows_trigger_a201"),
        "automation_workflows",
        "(trigger_type = 'manual' AND interval_minutes IS NULL) OR "
        "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)",
    )
