"""Step AG (HR) slice 1: pure leave-balance arithmetic.

Stdlib only — no DB, no network, no app imports. Balances are always
computed from records (entitled + carried - used - pending); a request
never carries its own "remaining balance" — callers pass the computed
:class:`LeaveBalance` in.

Weekend days are configurable per org (Pakistan companies commonly use
Sat+Sun; some work Saturday). Holidays are an explicit set of dates.
Leave dates are plain dates (Asia/Karachi org day); a half-day is 0.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# Python weekday(): Monday=0 ... Saturday=5, Sunday=6.
DEFAULT_WEEKEND_DAYS = frozenset({5, 6})

HALF_DAY = 0.5


@dataclass(frozen=True)
class LeavePolicy:
    """One leave type's yearly policy for one org."""

    leave_type: str  # "annual" | "sick" | "casual" (free-form otherwise)
    entitlement_days: float
    carry_forward_cap: float = 0.0
    max_consecutive_days: float | None = None
    accrual: str = "upfront"  # "upfront" | "prorata"
    weekend_days: frozenset = field(default=DEFAULT_WEEKEND_DAYS)


@dataclass(frozen=True)
class LeaveBalance:
    """Computed balance for one employee, one leave type, one year."""

    entitled: float
    carried: float
    used: float  # approved leave already taken
    pending: float  # approved-by-nobody-yet requests awaiting decision

    @property
    def remaining(self) -> float:
        return self.entitled + self.carried - self.used - self.pending


@dataclass(frozen=True)
class LeaveDecision:
    allowed: bool
    days: float
    remaining_after: float
    reasons: tuple[str, ...] = ()


def count_business_days(
    start: date,
    end: date,
    *,
    holidays: frozenset | set | None = None,
    weekend_days: frozenset | set | None = None,
    half_day: bool = False,
) -> float:
    """Business days in [start, end], inclusive.

    Weekends and holidays are skipped. Returns 0.0 when the range holds no
    business day. ``half_day`` is only meaningful for a single business
    day (validated by :func:`check_request`); here it simply halves a
    one-day count.
    """
    if end < start:
        return 0.0
    holidays = holidays or frozenset()
    weekend_days = DEFAULT_WEEKEND_DAYS if weekend_days is None else weekend_days
    days = 0
    day = start
    while day <= end:
        if day.weekday() not in weekend_days and day not in holidays:
            days += 1
        day += timedelta(days=1)
    if half_day:
        return HALF_DAY if days == 1 else float(days)
    return float(days)


def prorated_entitlement(policy: LeavePolicy, join_date: date, year: int) -> float:
    """Pro-rata entitlement for someone who joined mid-year.

    Upfront policies return the full entitlement. Pro-rata counts whole
    months remaining in the year (joining on the 1st counts that month),
    rounded to one decimal. Someone who joined before the year gets the
    full entitlement; someone joining after the year gets 0.
    """
    if policy.accrual != "prorata":
        return float(policy.entitlement_days)
    if join_date.year > year:
        return 0.0
    if join_date.year < year:
        return float(policy.entitlement_days)
    months_left = 12 - join_date.month + (1 if join_date.day == 1 else 0)
    if months_left <= 0:
        return 0.0
    return round(policy.entitlement_days * months_left / 12, 1)


def check_request(
    policy: LeavePolicy,
    balance: LeaveBalance,
    start: date,
    end: date,
    *,
    today: date,
    holidays: frozenset | set | None = None,
    existing: tuple[tuple[date, date], ...] = (),
    half_day: bool = False,
) -> LeaveDecision:
    """Validate one leave request against policy, balance and history.

    ``existing`` is the employee's approved/pending ranges — any overlap
    denies the request. Reasons are stable machine-readable codes; the
    caller renders them for the user.
    """
    weekend_days = policy.weekend_days
    days = count_business_days(
        start, end, holidays=holidays, weekend_days=weekend_days, half_day=half_day
    )
    reasons: list[str] = []
    if end < start:
        reasons.append("end_before_start")
    if start < today:
        reasons.append("backdated")
    if not reasons and days <= 0:
        reasons.append("no_business_days")
    if half_day and days != HALF_DAY:
        reasons.append("half_day_single_day_only")
    if (
        policy.max_consecutive_days is not None
        and days > policy.max_consecutive_days
    ):
        reasons.append("exceeds_max_consecutive_days")
    if days > balance.remaining:
        reasons.append(f"insufficient_balance:shortfall={days - balance.remaining:g}")
    for ex_start, ex_end in existing:
        if start <= ex_end and ex_start <= end:
            reasons.append("overlaps_existing")
            break
    allowed = not reasons
    remaining_after = balance.remaining - days if allowed else balance.remaining
    return LeaveDecision(
        allowed=allowed,
        days=days,
        remaining_after=remaining_after,
        reasons=tuple(reasons),
    )


def apply_carry_forward(
    policy: LeavePolicy, unused_last_year: float
) -> float:
    """Carried days into the new year, capped by the policy (never negative)."""
    if unused_last_year <= 0:
        return 0.0
    return min(unused_last_year, policy.carry_forward_cap)
