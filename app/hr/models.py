"""Step AG (HR): leave policies/balances/requests, attendance, payroll schedules, salary reminders.

Every row is scoped to a department (`group_id`, the same scope as workflows and finance).
Permissions are evaluated on that department's platform scope: hr.leave.request,
hr.leave.approve, hr.attendance.record, hr.salary.remind, hr.manage. Leave days are
NUMERIC(5,1) (a half-day is 0.5); money is NUMERIC(18,2) and display-only — salary
reminders never move money.
"""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Time, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _group():
    return mapped_column(ForeignKey('access_groups.id', ondelete='RESTRICT'), nullable=False)


def _user():
    return mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)


def _ts():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


DAYS = Numeric(5, 1)
MONEY = Numeric(18, 2)

LEAVE_TYPES = ('annual', 'sick', 'casual')
LEAVE_STATUSES = ('pending', 'approved', 'rejected', 'cancelled')
ATTENDANCE_STATUSES = ('present', 'leave', 'holiday', 'work_from_home')
REMINDER_KINDS = ('upcoming', 'due_today', 'overdue')


class HrLeavePolicy(Base):
    """One leave type's yearly policy for one department (see app/hr/leave.py)."""

    __tablename__ = 'hr_leave_policies'
    __table_args__ = (
        UniqueConstraint('group_id', 'leave_type'),
        CheckConstraint("entitlement_days >= 0", name='entitlement_nonnegative'),
        CheckConstraint("carry_forward_cap >= 0", name='carry_forward_nonnegative'),
        CheckConstraint("accrual IN ('upfront','prorata')", name='accrual_valid'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    leave_type: Mapped[str] = mapped_column(String(32))
    entitlement_days: Mapped[Decimal] = mapped_column(DAYS)
    carry_forward_cap: Mapped[Decimal] = mapped_column(DAYS, default=Decimal('0'), server_default=text("'0'"))
    max_consecutive_days: Mapped[Decimal | None] = mapped_column(DAYS)
    accrual: Mapped[str] = mapped_column(String(16), default='upfront', server_default=text("'upfront'"))
    # Comma-separated Python weekday numbers (Monday=0 .. Sunday=6), e.g. "5,6" for Sat+Sun.
    weekend_days: Mapped[str] = mapped_column(String(16), default='5,6', server_default=text("'5,6'"))
    created_at: Mapped[datetime] = _ts()


class HrLeaveBalance(Base):
    """Derived balance cache for one employee, leave type and year.

    Recomputed from leave records (entitled + carried - used - pending); never
    trusted from input. A request carrying a "remaining balance" is ignored.
    """

    __tablename__ = 'hr_leave_balances'
    __table_args__ = (UniqueConstraint('group_id', 'employee_id', 'leave_type', 'year'),)
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    employee_id: Mapped[uuid.UUID] = _user()
    leave_type: Mapped[str] = mapped_column(String(32))
    year: Mapped[int] = mapped_column(Integer)
    entitled: Mapped[Decimal] = mapped_column(DAYS, default=Decimal('0'), server_default=text("'0'"))
    carried: Mapped[Decimal] = mapped_column(DAYS, default=Decimal('0'), server_default=text("'0'"))
    used: Mapped[Decimal] = mapped_column(DAYS, default=Decimal('0'), server_default=text("'0'"))
    pending: Mapped[Decimal] = mapped_column(DAYS, default=Decimal('0'), server_default=text("'0'"))
    updated_at: Mapped[datetime] = _ts()


class HrLeaveRequest(Base):
    """One leave request. Status moves pending -> approved/rejected by the
    department manager's exact-action approval, or pending -> cancelled by the
    requester. `applied_at` marks the balance update (via execute_once)."""

    __tablename__ = 'hr_leave_requests'
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name='status_valid'),
        CheckConstraint('end_date >= start_date', name='dates_ordered'),
        CheckConstraint('days > 0', name='days_positive'),
        Index('ix_hr_leave_requests_employee', 'group_id', 'employee_id', 'status'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    employee_id: Mapped[uuid.UUID] = _user()
    leave_type: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    half_day: Mapped[bool] = mapped_column(default=False, server_default=text('false'))
    days: Mapped[Decimal] = mapped_column(DAYS)
    status: Mapped[str] = mapped_column(String(16), default='pending', server_default=text("'pending'"))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('platform_approval_requests.id', ondelete='RESTRICT'))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class HrAttendance(Base):
    """Attendance is a record, not surveillance: dates and check-in/out times only."""

    __tablename__ = 'hr_attendance'
    __table_args__ = (
        UniqueConstraint('group_id', 'employee_id', 'date'),
        CheckConstraint("status IN ('present','leave','holiday','work_from_home')", name='status_valid'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    employee_id: Mapped[uuid.UUID] = _user()
    date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    check_in: Mapped[time | None] = mapped_column(Time)
    check_out: Mapped[time | None] = mapped_column(Time)
    created_at: Mapped[datetime] = _ts()


class HrPayrollSchedule(Base):
    """One employee's monthly pay schedule. Amounts are display-only."""

    __tablename__ = 'hr_payroll_schedules'
    __table_args__ = (
        UniqueConstraint('group_id', 'employee_id'),
        CheckConstraint('pay_day BETWEEN 1 AND 31', name='pay_day_valid'),
        CheckConstraint('amount >= 0', name='amount_nonnegative'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    employee_id: Mapped[uuid.UUID] = _user()
    pay_day: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default='PKR', server_default=text("'PKR'"))
    active: Mapped[bool] = mapped_column(default=True, server_default=text('true'))
    created_at: Mapped[datetime] = _ts()


class HrSalaryReminder(Base):
    """A generated salary reminder (notification only — no payment was made)."""

    __tablename__ = 'hr_salary_reminders'
    __table_args__ = (
        UniqueConstraint('payroll_schedule_id', 'pay_date', 'kind'),
        CheckConstraint("kind IN ('upcoming','due_today','overdue')", name='kind_valid'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    employee_id: Mapped[uuid.UUID] = _user()
    payroll_schedule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('hr_payroll_schedules.id', ondelete='RESTRICT'))
    pay_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    message: Mapped[str] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _ts()
