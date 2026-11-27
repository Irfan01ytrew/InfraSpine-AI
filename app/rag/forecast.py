"""Deterministic what-if projections. This module does not call an AI model or imply statistical confidence."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MODEL_VERSION = "linear-growth-v1"
_QUANTUM = Decimal("0.000001")
_MAX_ABS_VALUE = Decimal("1000000000000000000")
_NUMBER = re.compile(r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w.])")


def parse_finite_decimal(value: object, *, maximum_abs: Decimal = _MAX_ABS_VALUE) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Enter a finite numeric value.") from None
    if not number.is_finite() or abs(number) > maximum_abs:
        raise ValueError("The numeric value is outside the supported range.")
    return number


def numeric_tokens(text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in _NUMBER.finditer(text):
        try:
            values.add(Decimal(match.group(0).replace(",", "")))
        except InvalidOperation:
            continue
    return values


def next_period_label(last_period: str, period_unit: str, step: int) -> str:
    if period_unit == "year" and (match := re.fullmatch(r"(\d{4})", last_period)):
        return str(int(match.group(1)) + step)
    if period_unit == "quarter" and (match := re.fullmatch(r"(\d{4})-Q([1-4])", last_period, re.IGNORECASE)):
        ordinal = int(match.group(1)) * 4 + int(match.group(2)) - 1 + step
        return f"{ordinal // 4}-Q{ordinal % 4 + 1}"
    if period_unit == "month" and (match := re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", last_period)):
        ordinal = int(match.group(1)) * 12 + int(match.group(2)) - 1 + step
        return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"
    return f"Forecast +{step} {period_unit}"


def calculate_projection(
    observations: list[tuple[str, Decimal]],
    *,
    growth_rate_pct: Decimal,
    additive_adjustment: Decimal,
    horizon_periods: int,
    period_unit: str,
) -> list[dict[str, str]]:
    """Compound the explicit user assumption from the last historical value with a fixed per-period offset."""
    if len(observations) < 2 or len(observations) > 120:
        raise ValueError("A scenario requires between 2 and 120 historical observations.")
    if not 1 <= horizon_periods <= 36:
        raise ValueError("Forecast horizon must be from 1 to 36 periods.")
    if not Decimal("-100") <= growth_rate_pct <= Decimal("500"):
        raise ValueError("Growth assumption must be from -100% to 500% per period.")
    if len({period for period, _ in observations}) != len(observations):
        raise ValueError("Historical periods must be unique.")
    growth_factor = Decimal("1") + growth_rate_pct / Decimal("100")
    previous = observations[-1][1]
    results: list[dict[str, str]] = []
    for step in range(1, horizon_periods + 1):
        previous = (previous * growth_factor + additive_adjustment).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
        if not previous.is_finite() or abs(previous) > _MAX_ABS_VALUE:
            raise ValueError("The forecast result exceeds the supported numeric range.")
        results.append({
            "period": next_period_label(observations[-1][0], period_unit, step),
            "value": format(previous, "f"),
        })
    return results
