"""Allow approved workflows to assign tasks into My Work.

Revision ID: 0065_workflow_personal_task_action
Revises: 0064_durable_agent_run_queue
"""

from alembic import op
import sqlalchemy as sa

revision = "0065_workflow_personal_task_action"
down_revision = "0064_durable_agent_run_queue"
branch_labels = None
depends_on = None


def _install_run_guard(*, include_personal_task: bool) -> None:
    extra_completion_evidence = (
        " AND NOT EXISTS (SELECT 1 FROM personal_tasks WHERE automation_run_id = NEW.id)"
        if include_personal_task
        else ""
    )
    op.execute(
        f"""CREATE OR REPLACE FUNCTION automation_runs_guard() RETURNS trigger AS $$
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
            IF NEW.status = 'completed'
               AND NOT EXISTS (SELECT 1 FROM automation_tasks WHERE run_id = NEW.id)
               AND NOT EXISTS (
                   SELECT 1 FROM workflow_notifications
                   WHERE run_id = NEW.id AND notification_type = 'workflow_message'
               ){extra_completion_evidence} THEN
                RAISE EXCEPTION 'completed workflow runs require a durable task or message' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )


def upgrade() -> None:
    op.add_column(
        "personal_tasks",
        sa.Column(
            "automation_run_id",
            sa.UUID(),
            sa.ForeignKey("automation_runs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        op.f("uq_personal_tasks_automation_run_id"),
        "personal_tasks",
        ["automation_run_id"],
    )
    op.drop_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        "action_type IN ('create_task', 'notify_member', 'assign_personal_task')",
    )
    _install_run_guard(include_personal_task=True)


def downgrade() -> None:
    bind = op.get_bind()
    linked_tasks = bind.execute(
        sa.text("SELECT count(*) FROM personal_tasks WHERE automation_run_id IS NOT NULL")
    ).scalar_one()
    personal_action_workflows = bind.execute(
        sa.text("SELECT count(*) FROM automation_workflows WHERE action_type = 'assign_personal_task'")
    ).scalar_one()
    if linked_tasks or personal_action_workflows:
        raise RuntimeError(
            "Cannot remove workflow personal-task support while linked tasks or action definitions remain."
        )

    _install_run_guard(include_personal_task=False)
    op.drop_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_automation_workflows_action_allowlisted"),
        "automation_workflows",
        "action_type IN ('create_task', 'notify_member')",
    )
    op.drop_constraint(
        op.f("uq_personal_tasks_automation_run_id"),
        "personal_tasks",
        type_="unique",
    )
    op.drop_column("personal_tasks", "automation_run_id")
