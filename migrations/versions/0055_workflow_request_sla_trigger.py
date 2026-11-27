"""Allow approval-gated workflows on department response-SLA escalation.

Revision ID: 0055_workflow_sla_trigger
Revises: 0054_partner_document_index
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_workflow_sla_trigger"
down_revision = "0054_partner_document_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "automation_workflows"
    op.drop_constraint(op.f("ck_automation_workflows_trigger_valid"), table, type_="check")
    op.drop_constraint(op.f("ck_automation_workflows_trigger_interval_valid"), table, type_="check")
    op.create_check_constraint(op.f("ck_automation_workflows_trigger_valid"), table,
                               "trigger_type IN ('manual', 'interval', 'service_request', 'request_sla')")
    op.create_check_constraint(op.f("ck_automation_workflows_trigger_interval_valid"), table,
        "(trigger_type IN ('manual', 'service_request', 'request_sla') AND interval_minutes IS NULL) OR "
        "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM automation_workflows WHERE trigger_type='request_sla')")):
        raise RuntimeError("Remove or export response-SLA workflows before downgrading migration 0055.")
    table = "automation_workflows"
    op.drop_constraint(op.f("ck_automation_workflows_trigger_interval_valid"), table, type_="check")
    op.drop_constraint(op.f("ck_automation_workflows_trigger_valid"), table, type_="check")
    op.create_check_constraint(op.f("ck_automation_workflows_trigger_valid"), table,
                               "trigger_type IN ('manual', 'interval', 'service_request')")
    op.create_check_constraint(op.f("ck_automation_workflows_trigger_interval_valid"), table,
        "(trigger_type IN ('manual', 'service_request') AND interval_minutes IS NULL) OR "
        "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)")
