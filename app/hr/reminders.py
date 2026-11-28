"""Step AG (HR) slice 1: salary-reminder helper.

Pure stdlib. A reminder is a notification only — it never moves money,
never changes a payee, and never edits an amount. Amounts are copied
from the payroll schedule into the reminder text.

One reminder per schedule per pay cycle: ``upcoming`` in the advance
window before pay-day, ``due_today`` on pay-day, ``overdue`` after
pay-day until the next cycle starts. Inactive schedules never remind.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

DEFAULT_ADVANCE_DAYS = 3


@dataclass(frozen=True)
class PayrollSchedule:
    """One employee's monthly pay schedule (display data only)."""

    employee_id: str
    pay_day: int  # day of month, 1..31; clamped to the month's length
    amount: Decimal
    currency: str = "PKR"
    active: bool = True


@dataclass(frozen=True)
class SalaryReminder:
    employee_id: str
    kind: str  # "upcoming" | "due_today" | "overdue"
    pay_date: date
    amount: Decimal
    currency: str
    message: str


def _pay_date_for(year: int, month: int, pay_day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(pay_day, 1), last_day))


def salary_reminders(
    schedules: list[PayrollSchedule],
    today: date,
    *,
    advance_days: int = DEFAULT_ADVANCE_DAYS,
) -> list[SalaryReminder]:
    """Reminders due today for each active schedule, at most one each."""
    reminders: list[SalaryReminder] = []
    for schedule in schedules:
        if not schedule.active:
            continue
        pay_date = _pay_date_for(today.year, today.month, schedule.pay_day)
        if today < pay_date - timedelta(days=advance_days):
            continue
        if today < pay_date:
            kind = "upcoming"
        elif today == pay_date:
            kind = "due_today"
        else:
            kind = "overdue"
        if kind == "upcoming":
            when = f"due on {pay_date.isoformat()} (in {(pay_date - today).days} day(s))"
        elif kind == "due_today":
            when = f"due today ({pay_date.isoformat()})"
        else:
            when = f"was due on {pay_date.isoformat()} ({(today - pay_date).days} day(s) ago)"
        reminders.append(
            SalaryReminder(
                employee_id=schedule.employee_id,
                kind=kind,
                pay_date=pay_date,
                amount=schedule.amount,
                currency=schedule.currency,
                message=(
                    f"Salary of {schedule.currency} {schedule.amount} "
                    f"for {schedule.employee_id} {when}. "
                    "Reminder only — no payment was made."
                ),
            )
        )
    return reminders
