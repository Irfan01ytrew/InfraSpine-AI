"""Step J (AI Workforce OS), slice 5: budget rules (pure functions).

Pure functions (stdlib only). No database, no routes, no production code
changed. These rules decide whether an agent may start work given what
it has already spent; they never execute anything and never grant
anything.

Money is ``Decimal`` only, never float. Prices are never invented here:
spend is the recorded cost of work already done (Agent Studio runs carry
``estimated_cost_usd`` from the provider pricing table; Step T's cost
estimates feed the same field when that branch merges). A missing limit
(``None``) means "no limit set", not "free" and not "zero".

Defaults (plan §7.1, §7.5 — safe defaults, the owner may change them):

* Per-agent monthly spend limit: USD 10 (the AK proposal, adopted for J).
* Per-agent daily spend limit: none.
* Per-agent daily / monthly call limits: none.
* Org quotas (max agents, total monthly spend): none until the owner
  sets them — per-agent limits still apply.

Windows are UTC calendar windows: a day runs 00:00–24:00 UTC, a month
runs the 1st 00:00 UTC to the 1st of the next month 00:00 UTC.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

__all__ = [
    "DEFAULT_MONTHLY_SPEND_LIMIT_USD",
    "BudgetLimits",
    "BudgetCheck",
    "check_budget",
    "day_window",
    "month_window",
    "validate_budget_limits",
]

DEFAULT_MONTHLY_SPEND_LIMIT_USD = Decimal("10")


def _money(value: object) -> Decimal | None:
    """Coerce to Decimal, or None when the value is not a number.

    ``None`` input means "no value set"; a non-numeric value is also
    None. Booleans are not money.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar day containing ``now``: [start, end)."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``now``: [start, end)."""
    start = now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = monthrange(start.year, start.month)[1]
    end = start.replace(day=last_day) + timedelta(days=1)
    return start, end


@dataclass(frozen=True)
class BudgetLimits:
    """Per-agent (or org) limits. ``None`` means no limit set."""

    daily_spend_limit_usd: Decimal | None = None
    monthly_spend_limit_usd: Decimal | None = None
    daily_call_limit: int | None = None
    monthly_call_limit: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "daily_spend_limit_usd", _money(self.daily_spend_limit_usd))
        object.__setattr__(
            self, "monthly_spend_limit_usd", _money(self.monthly_spend_limit_usd))


@dataclass(frozen=True)
class BudgetCheck:
    """The budget verdict: allowed, or denied with a reason code.

    Reasons: ``"daily_spend_exceeded"``, ``"monthly_spend_exceeded"``,
    ``"daily_call_limit_exceeded"``, ``"monthly_call_limit_exceeded"``,
    ``"org_monthly_spend_exceeded"``, ``"org_agent_quota_exceeded"``,
    ``"invalid_usage:<detail>"``.
    """

    allowed: bool
    reason: str = ""


def validate_budget_limits(limits: BudgetLimits) -> tuple[str, ...]:
    """Structural problems in a limit set, as human-readable strings.

    An empty tuple means the limits are sound. This never raises.
    """
    problems: list[str] = []
    for name in ("daily_spend_limit_usd", "monthly_spend_limit_usd"):
        value = getattr(limits, name)
        if value is not None and (not isinstance(value, Decimal) or value < 0):
            problems.append(f"{name} must be a non-negative amount")
    for name in ("daily_call_limit", "monthly_call_limit"):
        value = getattr(limits, name)
        if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0):
            problems.append(f"{name} must be a non-negative integer")
    return tuple(problems)


def check_budget(
    *,
    spent_today_usd: Decimal,
    spent_this_month_usd: Decimal,
    calls_today: int,
    calls_this_month: int,
    limits: BudgetLimits,
    org_spent_this_month_usd: Decimal | None = None,
    org_monthly_spend_limit_usd: Decimal | None = None,
    org_agent_count: int | None = None,
    org_max_agents: int | None = None,
) -> BudgetCheck:
    """Decide whether the next start is allowed within the limits.

    Limits are checked cheapest-first; the first exceeded limit is the
    recorded reason. A spent amount that is not a number, or a call
    count that is negative, is invalid usage — deny, never guess.
    Never raises on bad inputs.
    """
    spent_today = _money(spent_today_usd)
    spent_month = _money(spent_this_month_usd)
    org_spent = _money(org_spent_this_month_usd)
    org_limit = _money(org_monthly_spend_limit_usd)
    if spent_today is None or spent_month is None:
        return BudgetCheck(allowed=False, reason="invalid_usage:bad_spend")
    if spent_today < 0 or spent_month < 0:
        return BudgetCheck(allowed=False, reason="invalid_usage:negative_spend")
    for name, count in (("calls_today", calls_today), ("calls_this_month", calls_this_month)):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return BudgetCheck(allowed=False, reason=f"invalid_usage:bad_{name}")
    if limits.daily_spend_limit_usd is not None \
            and spent_today >= limits.daily_spend_limit_usd:
        return BudgetCheck(allowed=False, reason="daily_spend_exceeded")
    if limits.monthly_spend_limit_usd is not None \
            and spent_month >= limits.monthly_spend_limit_usd:
        return BudgetCheck(allowed=False, reason="monthly_spend_exceeded")
    if limits.daily_call_limit is not None and calls_today >= limits.daily_call_limit:
        return BudgetCheck(allowed=False, reason="daily_call_limit_exceeded")
    if limits.monthly_call_limit is not None \
            and calls_this_month >= limits.monthly_call_limit:
        return BudgetCheck(allowed=False, reason="monthly_call_limit_exceeded")
    if org_limit is not None and org_spent is not None and org_spent >= org_limit:
        return BudgetCheck(allowed=False, reason="org_monthly_spend_exceeded")
    if org_max_agents is not None and org_agent_count is not None \
            and org_agent_count > org_max_agents:
        return BudgetCheck(allowed=False, reason="org_agent_quota_exceeded")
    return BudgetCheck(allowed=True, reason="")
