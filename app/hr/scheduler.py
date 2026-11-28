"""Step AG (HR) slice 5: salary-reminder scheduler.

Read-only builder + idempotent row creation. The cycle loads every active
payroll schedule, computes the reminders due today with the pure helper
(:mod:`app.hr.reminders`), and stores one :class:`HrSalaryReminder` row per
schedule per pay cycle (unique on schedule + pay date + kind, so running the
cycle twice creates nothing new). Amounts are copied from the schedule,
never edited; no payment is made, no payee changes.

Delivery boundary (honest): rows are created with ``sent_at=None`` and
``channel=None``. Sending them (WhatsApp/email) is Step AA's scheduled-report
delivery, which is not on this branch. When AA merges, its delivery worker
claims rows with ``sent_at IS NULL`` and stamps ``sent_at``/``channel``.
The HR pages show unsent rows as "pending delivery".
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.hr.models import HrPayrollSchedule, HrSalaryReminder
from app.hr.reminders import PayrollSchedule, salary_reminders


async def run_salary_reminder_cycle(db, today: date, *, group_id: uuid.UUID | None = None) -> dict:
    """Create this cycle's salary reminders. Idempotent; safe to run twice a day.

    ``group_id`` scopes the cycle to one department (the API trigger); with
    ``group_id=None`` the cycle covers every department (the worker).
    Returns ``{'created': n, 'already_present': n, 'due': n}``.
    """
    query = select(HrPayrollSchedule).where(HrPayrollSchedule.active.is_(True))
    if group_id is not None:
        query = query.where(HrPayrollSchedule.group_id == group_id)
    schedules = (await db.scalars(query.order_by(HrPayrollSchedule.group_id, HrPayrollSchedule.employee_id))).all()
    due = salary_reminders(
        [PayrollSchedule(employee_id=str(s.employee_id), pay_day=s.pay_day, amount=Decimal(str(s.amount)),
                         currency=s.currency, active=s.active) for s in schedules],
        today,
    )
    by_employee = {str(s.employee_id): s for s in schedules}
    created = 0
    for reminder in due:
        schedule = by_employee[reminder.employee_id]
        result = await db.execute(
            pg_insert(HrSalaryReminder).values(
                group_id=schedule.group_id, employee_id=schedule.employee_id,
                payroll_schedule_id=schedule.id, pay_date=reminder.pay_date, kind=reminder.kind,
                amount=reminder.amount, currency=reminder.currency, message=reminder.message,
            ).on_conflict_do_nothing(
                index_elements=["payroll_schedule_id", "pay_date", "kind"]))
        created += result.rowcount or 0
    await db.commit()
    return {"created": created, "already_present": len(due) - created, "due": len(due)}
