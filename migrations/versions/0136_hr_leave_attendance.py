"""Step AG: HR leave policies/balances/requests, attendance, payroll schedules, salary reminders.

Revision ID: 0136_hr_leave_attendance
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0136_hr_leave_attendance"
down_revision: str | None = "0135_whatsapp_leads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hr_leave_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_type", sa.String(32), nullable=False),
        sa.Column("entitlement_days", sa.Numeric(5, 1), nullable=False),
        sa.Column("carry_forward_cap", sa.Numeric(5, 1), nullable=False, server_default="0"),
        sa.Column("max_consecutive_days", sa.Numeric(5, 1), nullable=True),
        sa.Column("accrual", sa.String(16), nullable=False, server_default="upfront"),
        sa.Column("weekend_days", sa.String(16), nullable=False, server_default="5,6"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("entitlement_days >= 0", name="entitlement_nonnegative"),
        sa.CheckConstraint("carry_forward_cap >= 0", name="carry_forward_nonnegative"),
        sa.CheckConstraint("accrual IN ('upfront','prorata')", name="accrual_valid"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "leave_type"),
    )
    op.create_table(
        "hr_leave_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_type", sa.String(32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("entitled", sa.Numeric(5, 1), nullable=False, server_default="0"),
        sa.Column("carried", sa.Numeric(5, 1), nullable=False, server_default="0"),
        sa.Column("used", sa.Numeric(5, 1), nullable=False, server_default="0"),
        sa.Column("pending", sa.Numeric(5, 1), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "employee_id", "leave_type", "year"),
    )
    op.create_table(
        "hr_leave_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_type", sa.String(32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("half_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("days", sa.Numeric(5, 1), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("workflow_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name="status_valid"),
        sa.CheckConstraint("end_date >= start_date", name="dates_ordered"),
        sa.CheckConstraint("days > 0", name="days_positive"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approval_id"], ["platform_approval_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hr_leave_requests_employee", "hr_leave_requests", ["group_id", "employee_id", "status"])
    op.create_table(
        "hr_attendance",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("check_in", sa.Time(), nullable=True),
        sa.Column("check_out", sa.Time(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('present','leave','holiday','work_from_home')", name="status_valid"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "employee_id", "date"),
    )
    op.create_table(
        "hr_payroll_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pay_day", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="PKR"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("pay_day BETWEEN 1 AND 31", name="pay_day_valid"),
        sa.CheckConstraint("amount >= 0", name="amount_nonnegative"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "employee_id"),
    )
    op.create_table(
        "hr_salary_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payroll_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('upcoming','due_today','overdue')", name="kind_valid"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payroll_schedule_id"], ["hr_payroll_schedules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payroll_schedule_id", "pay_date", "kind"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM hr_leave_requests)")):
        raise RuntimeError("Leave requests exist. Restore a verified backup instead of downgrading.")
    op.drop_table("hr_salary_reminders")
    op.drop_table("hr_payroll_schedules")
    op.drop_table("hr_attendance")
    op.drop_index("ix_hr_leave_requests_employee", table_name="hr_leave_requests")
    op.drop_table("hr_leave_requests")
    op.drop_table("hr_leave_balances")
    op.drop_table("hr_leave_policies")
