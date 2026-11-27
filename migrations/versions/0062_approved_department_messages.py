"""Add the approved, department-scoped workflow inbox-message action.

Revision ID: 0062_approved_department_messages
Revises: 0061_persist_cloud_connectors
"""

from alembic import op
import sqlalchemy as sa

revision = "0062_approved_department_messages"
down_revision = "0061_persist_cloud_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0025 supplied an already-prefixed name to a metadata naming convention, so
    # deployed databases can have a convention-expanded constraint name. Find the
    # single legacy action check by its expression instead of guessing that name.
    op.execute(
        """DO $$ DECLARE constraint_row RECORD;
        BEGIN
            FOR constraint_row IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'automation_workflows'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) ILIKE '%action_type%'
                  AND pg_get_constraintdef(oid) ILIKE '%create_task%'
            LOOP
                EXECUTE format('ALTER TABLE automation_workflows DROP CONSTRAINT %I', constraint_row.conname);
            END LOOP;
        END $$"""
    )
    op.create_check_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        "action_type IN ('create_task', 'notify_member')",
    )

    op.add_column("workflow_notifications", sa.Column("message_title", sa.String(length=180), nullable=True))
    op.add_column("workflow_notifications", sa.Column("message_body", sa.String(length=2000), nullable=True))
    op.drop_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"),
        "workflow_notifications",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"),
        "workflow_notifications",
        "notification_type IN ('run_approved', 'run_rejected', 'task_assigned', 'approval_required', 'workflow_message')",
    )
    op.drop_constraint(
        op.f("ck_workflow_notifications_task_reference_valid"),
        "workflow_notifications",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_notifications_task_reference_valid"),
        "workflow_notifications",
        "(notification_type = 'task_assigned' AND task_id IS NOT NULL AND message_title IS NULL AND message_body IS NULL) OR "
        "(notification_type = 'workflow_message' AND task_id IS NULL AND message_title IS NOT NULL AND message_body IS NOT NULL) OR "
        "(notification_type IN ('run_approved', 'run_rejected', 'approval_required') AND task_id IS NULL AND message_title IS NULL AND message_body IS NULL)",
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION automation_runs_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workflow runs are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending_approval' OR NEW.attempts <> 0 THEN
                    RAISE EXCEPTION 'workflow runs must begin pending approval' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id, NEW.workflow_id, NEW.requester_user_id, NEW.department_group_id,
                   NEW.idempotency_key, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.workflow_id, OLD.requester_user_id, OLD.department_group_id,
                   OLD.idempotency_key, OLD.created_at) THEN
                RAISE EXCEPTION 'workflow run identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'pending_approval' AND NEW.status IN ('queued', 'rejected', 'cancelled')) OR
                (OLD.status = 'queued' AND NEW.status IN ('processing', 'cancelled')) OR
                (OLD.status = 'processing' AND NEW.status IN ('queued', 'completed', 'failed')) OR
                (OLD.status = 'failed' AND NEW.status = 'queued')
            ) THEN
                RAISE EXCEPTION 'invalid workflow run status transition' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.attempts > OLD.attempts + 1 OR (
                NEW.attempts < OLD.attempts AND NOT (OLD.status = 'failed' AND NEW.status = 'queued' AND NEW.attempts = 0)
            ) THEN
                RAISE EXCEPTION 'invalid workflow attempt counter change' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'queued' AND NOT EXISTS (
                SELECT 1 FROM automation_approvals WHERE run_id = NEW.id AND decision = 'approved'
            ) THEN
                RAISE EXCEPTION 'queued workflow runs require an approval' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'rejected' AND NOT EXISTS (
                SELECT 1 FROM automation_approvals WHERE run_id = NEW.id AND decision = 'rejected'
            ) THEN
                RAISE EXCEPTION 'rejected workflow runs require a rejection decision' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'completed' AND NOT EXISTS (
                SELECT 1 FROM automation_tasks WHERE run_id = NEW.id
            ) AND NOT EXISTS (
                SELECT 1 FROM workflow_notifications
                WHERE run_id = NEW.id AND notification_type = 'workflow_message'
            ) THEN
                RAISE EXCEPTION 'completed workflow runs require a durable task or message' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM workflow_notifications WHERE notification_type = 'workflow_message')
               OR EXISTS (SELECT 1 FROM automation_workflows WHERE action_type = 'notify_member') THEN
                RAISE EXCEPTION 'workflow messages or definitions exist; archive them before downgrade';
            END IF;
        END $$"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION automation_runs_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workflow runs are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending_approval' OR NEW.attempts <> 0 THEN
                    RAISE EXCEPTION 'workflow runs must begin pending approval' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id, NEW.workflow_id, NEW.requester_user_id, NEW.department_group_id,
                   NEW.idempotency_key, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.workflow_id, OLD.requester_user_id, OLD.department_group_id,
                   OLD.idempotency_key, OLD.created_at) THEN
                RAISE EXCEPTION 'workflow run identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'pending_approval' AND NEW.status IN ('queued', 'rejected', 'cancelled')) OR
                (OLD.status = 'queued' AND NEW.status IN ('processing', 'cancelled')) OR
                (OLD.status = 'processing' AND NEW.status IN ('queued', 'completed', 'failed')) OR
                (OLD.status = 'failed' AND NEW.status = 'queued')
            ) THEN
                RAISE EXCEPTION 'invalid workflow run status transition' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.attempts > OLD.attempts + 1 OR (
                NEW.attempts < OLD.attempts AND NOT (OLD.status = 'failed' AND NEW.status = 'queued' AND NEW.attempts = 0)
            ) THEN
                RAISE EXCEPTION 'invalid workflow attempt counter change' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'queued' AND NOT EXISTS (
                SELECT 1 FROM automation_approvals WHERE run_id = NEW.id AND decision = 'approved'
            ) THEN
                RAISE EXCEPTION 'queued workflow runs require an approval' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'rejected' AND NOT EXISTS (
                SELECT 1 FROM automation_approvals WHERE run_id = NEW.id AND decision = 'rejected'
            ) THEN
                RAISE EXCEPTION 'rejected workflow runs require a rejection decision' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'completed' AND NOT EXISTS (
                SELECT 1 FROM automation_tasks WHERE run_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'completed workflow runs require a durable task' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.drop_constraint(
        op.f("ck_workflow_notifications_task_reference_valid"),
        "workflow_notifications",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_notifications_task_reference_valid"),
        "workflow_notifications",
        "(notification_type = 'task_assigned' AND task_id IS NOT NULL) OR "
        "(notification_type IN ('run_approved', 'run_rejected', 'approval_required') AND task_id IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"),
        "workflow_notifications",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_notifications_notification_type_valid"),
        "workflow_notifications",
        "notification_type IN ('run_approved', 'run_rejected', 'task_assigned', 'approval_required')",
    )
    op.drop_column("workflow_notifications", "message_body")
    op.drop_column("workflow_notifications", "message_title")
    op.drop_constraint(op.f("ck_automation_workflows_action_allowlisted"), "automation_workflows", type_="check")
    op.create_check_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        "action_type = 'create_task'",
    )
