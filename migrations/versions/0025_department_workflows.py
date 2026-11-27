"""Add approval-gated, department-scoped internal workflow tasks.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(length=32), server_default=sa.text("'create_task'"), nullable=False),
        sa.Column("action_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_automation_workflows_version_positive"),
        sa.CheckConstraint("trigger_type IN ('manual', 'interval')", name="ck_automation_workflows_trigger_valid"),
        sa.CheckConstraint("action_type = 'create_task'", name="ck_automation_workflows_action_allowlisted"),
        sa.CheckConstraint(
            "(trigger_type = 'manual' AND interval_minutes IS NULL) OR "
            "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)",
            name="ck_automation_workflows_trigger_interval_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(action_config) = 'object'", name="ck_automation_workflows_action_config_object"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("definition_key", "version", name="uq_automation_workflows_key_version"),
    )
    op.create_index(
        "uq_automation_workflows_one_enabled_version",
        "automation_workflows",
        ["definition_key"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )
    op.create_index("ix_automation_workflows_due", "automation_workflows", ["enabled", "next_run_at"])

    op.create_table(
        "automation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'pending_approval'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'queued', 'processing', 'completed', 'rejected', 'cancelled', 'failed')",
            name="ck_automation_runs_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_automation_runs_attempts_valid"),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="ck_automation_runs_completed_timestamp_valid",
        ),
        sa.ForeignKeyConstraint(["workflow_id"], ["automation_workflows.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency_key"),
    )
    op.create_index("ix_automation_runs_due", "automation_runs", ["status", "available_at"])
    op.create_index(
        "ix_automation_runs_department_created", "automation_runs", ["department_group_id", "created_at"]
    )

    op.create_table(
        "automation_approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approver_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="ck_automation_approvals_decision_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_automation_approvals_rationale_length"),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_automation_approvals_run"),
    )

    op.create_table(
        "automation_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("body", sa.String(length=4000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'completed')", name="ck_automation_tasks_status_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_automation_tasks_title_length"),
        sa.CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="ck_automation_tasks_body_length"),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="ck_automation_tasks_completion_timestamp_valid",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_automation_tasks_run"),
    )
    op.create_index("ix_automation_tasks_assignee_created", "automation_tasks", ["assignee_user_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION automation_workflows_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workflow versions are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF ROW(NEW.id, NEW.definition_key, NEW.version, NEW.name, NEW.department_group_id,
                   NEW.trigger_type, NEW.interval_minutes, NEW.action_type, NEW.action_config,
                   NEW.created_by_user_id, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.definition_key, OLD.version, OLD.name, OLD.department_group_id,
                   OLD.trigger_type, OLD.interval_minutes, OLD.action_type, OLD.action_config,
                   OLD.created_by_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'workflow version configuration is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT OLD.enabled AND NEW.enabled THEN
                RAISE EXCEPTION 'disabled workflow versions cannot be re-enabled' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER automation_workflows_guard
        BEFORE UPDATE OR DELETE ON automation_workflows
        FOR EACH ROW EXECUTE FUNCTION automation_workflows_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION automation_runs_guard() RETURNS trigger AS $$
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
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER automation_runs_guard
        BEFORE INSERT OR UPDATE OR DELETE ON automation_runs
        FOR EACH ROW EXECUTE FUNCTION automation_runs_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION automation_approvals_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'workflow approvals are append-only' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM automation_runs r
                WHERE r.id = NEW.run_id AND r.status = 'pending_approval'
                  AND r.requester_user_id IS DISTINCT FROM NEW.approver_user_id
            ) THEN
                RAISE EXCEPTION 'approval requires a different approver and a pending run' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM users u
                WHERE u.id = NEW.approver_user_id AND u.is_active AND (
                    u.role = 'admin' OR (
                        u.role = 'manager' AND EXISTS (
                            SELECT 1 FROM automation_runs r
                            JOIN department_managers dm ON dm.group_id = r.department_group_id
                            WHERE r.id = NEW.run_id AND dm.manager_user_id = u.id
                        )
                    )
                )
            ) THEN
                RAISE EXCEPTION 'approver must be an active department manager or administrator' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER automation_approvals_guard
        BEFORE UPDATE OR DELETE ON automation_approvals
        FOR EACH ROW EXECUTE FUNCTION automation_approvals_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION automation_tasks_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workflow tasks are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM automation_runs r
                    JOIN automation_approvals a ON a.run_id = r.id AND a.decision = 'approved'
                    JOIN access_group_members m ON m.group_id = r.department_group_id AND m.user_id = NEW.assignee_user_id
                    JOIN users u ON u.id = m.user_id AND u.is_active
                    WHERE r.id = NEW.run_id AND r.status = 'processing'
                      AND r.department_group_id = NEW.department_group_id
                ) THEN
                    RAISE EXCEPTION 'workflow task requires approved run and active department assignee' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id, NEW.run_id, NEW.assignee_user_id, NEW.department_group_id,
                   NEW.title, NEW.body, NEW.due_at, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.run_id, OLD.assignee_user_id, OLD.department_group_id,
                   OLD.title, OLD.body, OLD.due_at, OLD.created_at) THEN
                RAISE EXCEPTION 'workflow task content is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status = 'completed' AND NEW.status <> 'completed' THEN
                RAISE EXCEPTION 'completed workflow tasks cannot be reopened' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status = 'completed' AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                RAISE EXCEPTION 'task completion timestamps are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'completed' AND OLD.status <> 'open' THEN
                RAISE EXCEPTION 'only open workflow tasks can be completed' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'completed' AND NOT EXISTS (
                SELECT 1 FROM access_group_members m
                JOIN users u ON u.id = m.user_id AND u.is_active
                WHERE m.group_id = OLD.department_group_id AND m.user_id = OLD.assignee_user_id
            ) THEN
                RAISE EXCEPTION 'task completion requires current active department membership' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER automation_tasks_guard
        BEFORE INSERT OR UPDATE OR DELETE ON automation_tasks
        FOR EACH ROW EXECUTE FUNCTION automation_tasks_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM automation_workflows)
               OR EXISTS (SELECT 1 FROM automation_runs)
               OR EXISTS (SELECT 1 FROM automation_approvals)
               OR EXISTS (SELECT 1 FROM automation_tasks) THEN
                RAISE EXCEPTION 'export workflow definitions, runs, decisions and tasks before downgrading 0025';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS automation_tasks_guard ON automation_tasks")
    op.execute("DROP FUNCTION IF EXISTS automation_tasks_guard()")
    op.execute("DROP TRIGGER IF EXISTS automation_approvals_guard ON automation_approvals")
    op.execute("DROP FUNCTION IF EXISTS automation_approvals_guard()")
    op.execute("DROP TRIGGER IF EXISTS automation_runs_guard ON automation_runs")
    op.execute("DROP FUNCTION IF EXISTS automation_runs_guard()")
    op.execute("DROP TRIGGER IF EXISTS automation_workflows_guard ON automation_workflows")
    op.execute("DROP FUNCTION IF EXISTS automation_workflows_guard()")
    op.drop_index("ix_automation_tasks_assignee_created", table_name="automation_tasks")
    op.drop_table("automation_tasks")
    op.drop_table("automation_approvals")
    op.drop_index("ix_automation_runs_department_created", table_name="automation_runs")
    op.drop_index("ix_automation_runs_due", table_name="automation_runs")
    op.drop_table("automation_runs")
    op.drop_index("ix_automation_workflows_due", table_name="automation_workflows")
    op.drop_index("uq_automation_workflows_one_enabled_version", table_name="automation_workflows")
    op.drop_table("automation_workflows")
