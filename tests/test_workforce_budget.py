"""Step J (AI Workforce OS), slice 5: budget rules (pure tests).

Run: ``python -m pytest tests/test_workforce_budget.py -q -p no:cacheprovider --noconftest``
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.workforce.budget import (
    DEFAULT_MONTHLY_SPEND_LIMIT_USD,
    BudgetCheck,
    BudgetLimits,
    check_budget,
    day_window,
    month_window,
    validate_budget_limits,
)


def test_default_monthly_spend_limit_is_ten_dollars():
    assert DEFAULT_MONTHLY_SPEND_LIMIT_USD == Decimal("10")


def test_windows_are_utc_calendar_windows():
    now = datetime(2026, 11, 28, 14, 33, tzinfo=UTC)
    start, end = day_window(now)
    assert (start, end) == (
        datetime(2026, 11, 28, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 29, 0, 0, tzinfo=UTC))
    start, end = month_window(now)
    assert (start, end) == (
        datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 1, 0, 0, tzinfo=UTC))
    # February in a leap year ends on the 29th.
    start, end = month_window(datetime(2028, 2, 10, tzinfo=UTC))
    assert end == datetime(2028, 3, 1, 0, 0, tzinfo=UTC)


def test_no_limits_means_allowed():
    check = check_budget(
        spent_today_usd=Decimal("999"), spent_this_month_usd=Decimal("999"),
        calls_today=999, calls_this_month=999, limits=BudgetLimits())
    assert check == BudgetCheck(allowed=True, reason="")


def test_monthly_spend_limit_stops_the_next_start():
    limits = BudgetLimits(monthly_spend_limit_usd=Decimal("10"))
    under = check_budget(
        spent_today_usd=Decimal("1"), spent_this_month_usd=Decimal("9.99"),
        calls_today=0, calls_this_month=0, limits=limits)
    assert under.allowed is True
    at_cap = check_budget(
        spent_today_usd=Decimal("1"), spent_this_month_usd=Decimal("10"),
        calls_today=0, calls_this_month=0, limits=limits)
    assert at_cap == BudgetCheck(allowed=False, reason="monthly_spend_exceeded")
    over = check_budget(
        spent_today_usd=Decimal("1"), spent_this_month_usd=Decimal("10.01"),
        calls_today=0, calls_this_month=0, limits=limits)
    assert over.reason == "monthly_spend_exceeded"


def test_daily_spend_checked_before_monthly():
    limits = BudgetLimits(daily_spend_limit_usd=Decimal("1"),
                          monthly_spend_limit_usd=Decimal("10"))
    check = check_budget(
        spent_today_usd=Decimal("1"), spent_this_month_usd=Decimal("10"),
        calls_today=0, calls_this_month=0, limits=limits)
    assert check.reason == "daily_spend_exceeded"


def test_call_limits():
    limits = BudgetLimits(daily_call_limit=5, monthly_call_limit=100)
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=4, calls_this_month=99, limits=limits).allowed is True
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=5, calls_this_month=99, limits=limits).reason == "daily_call_limit_exceeded"
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=100, limits=limits).reason == "monthly_call_limit_exceeded"


def test_org_limits():
    limits = BudgetLimits()
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=0, limits=limits,
        org_spent_this_month_usd=Decimal("50"),
        org_monthly_spend_limit_usd=Decimal("50")).reason == "org_monthly_spend_exceeded"
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=0, limits=limits,
        org_agent_count=11, org_max_agents=10).reason == "org_agent_quota_exceeded"
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=0, limits=limits,
        org_agent_count=10, org_max_agents=10).allowed is True


def test_invalid_usage_denies_never_guesses():
    assert check_budget(
        spent_today_usd=Decimal("-1"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=0, limits=BudgetLimits()).reason == (
        "invalid_usage:negative_spend")
    assert check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=-1, calls_this_month=0, limits=BudgetLimits()).reason == (
        "invalid_usage:bad_calls_today")
    assert check_budget(
        spent_today_usd="not money", spent_this_month_usd=Decimal("0"),  # type: ignore[arg-type]
        calls_today=0, calls_this_month=0, limits=BudgetLimits()).reason == (
        "invalid_usage:bad_spend")


def test_validate_budget_limits():
    assert validate_budget_limits(BudgetLimits()) == ()
    assert validate_budget_limits(BudgetLimits(
        daily_spend_limit_usd=Decimal("5"), monthly_call_limit=10)) == ()
    assert validate_budget_limits(BudgetLimits(
        daily_spend_limit_usd=Decimal("-1"))) == (
        "daily_spend_limit_usd must be a non-negative amount",)
    assert validate_budget_limits(BudgetLimits(daily_call_limit=-2)) == (
        "daily_call_limit must be a non-negative integer",)
    assert validate_budget_limits(BudgetLimits(monthly_call_limit=True)) == (  # type: ignore[arg-type]
        "monthly_call_limit must be a non-negative integer",)


def test_zero_limits_stop_everything():
    limits = BudgetLimits(monthly_spend_limit_usd=Decimal("0"))
    check = check_budget(
        spent_today_usd=Decimal("0"), spent_this_month_usd=Decimal("0"),
        calls_today=0, calls_this_month=0, limits=limits)
    assert check.reason == "monthly_spend_exceeded"
