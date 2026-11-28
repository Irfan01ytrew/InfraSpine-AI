"""Step AG slice 1: leave-balance calculator + salary reminders.

Pure functions only (app/hr/leave.py, app/hr/reminders.py) — no DB, no
network. Run with a minimal pytest venv, --noconftest, log capture on.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.hr.leave import (
    LeaveBalance,
    LeavePolicy,
    apply_carry_forward,
    check_request,
    count_business_days,
    prorated_entitlement,
)
from app.hr.reminders import PayrollSchedule, salary_reminders

# November 2026: Nov 2 is a Monday. Nov 7-8 is Sat-Sun.
MON = date(2026, 11, 2)
FRI = date(2026, 11, 6)
SAT = date(2026, 11, 7)
SUN = date(2026, 11, 8)
NEXT_MON = date(2026, 11, 9)
TODAY = date(2026, 11, 1)  # a Sunday; requests start after it

ANNUAL = LeavePolicy(
    leave_type="annual", entitlement_days=20, carry_forward_cap=5,
    max_consecutive_days=10,
)
BALANCE = LeaveBalance(entitled=20, carried=3, used=5, pending=2)  # remaining 16


# --- business-day counting -------------------------------------------------

def test_work_week_counts_five_days():
    assert count_business_days(MON, FRI) == 5.0


def test_weekend_skipped():
    assert count_business_days(MON, NEXT_MON) == 6.0  # Mon-Fri + next Mon


def test_holiday_skipped():
    assert count_business_days(MON, FRI, holidays={date(2026, 11, 4)}) == 4.0


def test_single_day_and_reversed_range():
    assert count_business_days(MON, MON) == 1.0
    assert count_business_days(FRI, MON) == 0.0


def test_custom_weekend_friday_only():
    friday_off = frozenset({4})  # Friday only
    assert count_business_days(MON, FRI, weekend_days=friday_off) == 4.0


def test_half_day_single_business_day():
    assert count_business_days(MON, MON, half_day=True) == 0.5


# --- balance ---------------------------------------------------------------

def test_remaining_is_entitled_plus_carried_minus_used_minus_pending():
    assert BALANCE.remaining == 16.0


def test_carry_forward_capped_and_never_negative():
    assert apply_carry_forward(ANNUAL, 8) == 5.0
    assert apply_carry_forward(ANNUAL, 2) == 2.0
    assert apply_carry_forward(ANNUAL, -3) == 0.0


# --- pro-rata accrual ------------------------------------------------------

def test_prorata_upfront_policy_returns_full_entitlement():
    assert prorated_entitlement(ANNUAL, date(2026, 7, 1), 2026) == 20.0


def test_prorata_mid_year_joiner():
    prorata = LeavePolicy(
        leave_type="annual", entitlement_days=20, accrual="prorata"
    )
    assert prorated_entitlement(prorata, date(2026, 7, 1), 2026) == 10.0
    assert prorated_entitlement(prorata, date(2026, 7, 15), 2026) == 8.3
    assert prorated_entitlement(prorata, date(2025, 3, 1), 2026) == 20.0
    assert prorated_entitlement(prorata, date(2027, 1, 1), 2026) == 0.0


# --- request validation ----------------------------------------------------

def test_allowed_request_deducts_from_remaining():
    decision = check_request(ANNUAL, BALANCE, MON, FRI, today=TODAY)
    assert decision.allowed is True
    assert decision.days == 5.0
    assert decision.remaining_after == 11.0
    assert decision.reasons == ()


def test_insufficient_balance_names_shortfall():
    decision = check_request(
        ANNUAL, LeaveBalance(20, 0, 18, 0), MON, NEXT_MON, today=TODAY
    )
    assert decision.allowed is False
    assert any(r.startswith("insufficient_balance:shortfall=4") for r in decision.reasons)


def test_exceeds_max_consecutive_days():
    long_range = (date(2026, 11, 2), date(2026, 11, 20))  # 15 business days
    decision = check_request(ANNUAL, BALANCE, *long_range, today=TODAY)
    assert decision.allowed is False
    assert "exceeds_max_consecutive_days" in decision.reasons


def test_backdated_and_reversed_requests_denied():
    assert "backdated" in check_request(
        ANNUAL, BALANCE, date(2026, 10, 26), date(2026, 10, 30),
        today=TODAY,
    ).reasons
    assert "end_before_start" in check_request(
        ANNUAL, BALANCE, FRI, MON, today=TODAY
    ).reasons


def test_weekend_only_range_has_no_business_days():
    decision = check_request(ANNUAL, BALANCE, SAT, SUN, today=TODAY)
    assert decision.allowed is False
    assert "no_business_days" in decision.reasons


def test_overlap_with_existing_request_denied():
    decision = check_request(
        ANNUAL, BALANCE, date(2026, 11, 4), date(2026, 11, 6),
        today=TODAY,
        existing=((date(2026, 11, 5), date(2026, 11, 5)),),
    )
    assert decision.allowed is False
    assert "overlaps_existing" in decision.reasons


def test_non_overlapping_existing_request_allowed():
    decision = check_request(
        ANNUAL, BALANCE, MON, FRI, today=TODAY,
        existing=((date(2026, 11, 16), date(2026, 11, 17)),),
    )
    assert decision.allowed is True


def test_half_day_allowed_only_for_a_single_day():
    single = check_request(ANNUAL, BALANCE, MON, MON, today=TODAY, half_day=True)
    assert single.allowed is True
    assert single.days == 0.5
    assert single.remaining_after == 15.5
    multi = check_request(ANNUAL, BALANCE, MON, FRI, today=TODAY, half_day=True)
    assert multi.allowed is False
    assert "half_day_single_day_only" in multi.reasons


# --- salary reminders ------------------------------------------------------

def _schedule(pay_day=28, active=True):
    return PayrollSchedule(
        employee_id="emp-1", pay_day=pay_day,
        amount=Decimal("150000"), currency="PKR", active=active,
    )


def test_reminder_upcoming_in_advance_window():
    reminders = salary_reminders([_schedule()], date(2026, 11, 25))
    assert len(reminders) == 1
    assert reminders[0].kind == "upcoming"
    assert reminders[0].pay_date == date(2026, 11, 28)
    assert "no payment was made" in reminders[0].message


def test_reminder_due_today_and_overdue():
    assert salary_reminders([_schedule()], date(2026, 11, 28))[0].kind == "due_today"
    assert salary_reminders([_schedule()], date(2026, 11, 29))[0].kind == "overdue"


def test_no_reminder_too_early_or_for_inactive_schedule():
    assert salary_reminders([_schedule()], date(2026, 11, 20)) == []
    assert salary_reminders([_schedule(active=False)], date(2026, 11, 28)) == []


def test_pay_day_clamped_to_month_length():
    reminders = salary_reminders([_schedule(pay_day=31)], date(2026, 2, 25))
    assert len(reminders) == 1
    assert reminders[0].pay_date == date(2026, 2, 28)  # Feb 2026 has 28 days


def test_amount_is_copied_never_edited():
    reminder = salary_reminders([_schedule()], date(2026, 11, 28))[0]
    assert reminder.amount == Decimal("150000")
    assert reminder.currency == "PKR"
    assert "150000" in reminder.message
