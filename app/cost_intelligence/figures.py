"""Pure cost-intelligence figures (Step N, slice 1).

Deterministic, stdlib-only computation of the v2 feature-13 figures from
recorded run/ledger rows:

- per-agent / per-department / org-wide totals, success and failure rates,
- cost per task and cost per successful task,
- spend vs budget (ok / warn at >=80% / over),
- human-vs-AI comparison with an honest ``unknown`` state.

Rules that never change:

- Money is :class:`decimal.Decimal`, never float. A run with no configured
  rate contributes to ``cost_unknown_count`` and never to a zero cost.
- A missing human-time input (minutes or rate) makes the human cost and the
  ROI ``unknown`` — never zero, never imputed.
- Figures are computed per scope; a roll-up groups by scope id and never mixes
  scopes (one department cannot see another's spend — the API enforces that
  with ``app/platform``; this module only guarantees the grouping).
- Text is untrusted data. ``RunRecord.notes`` (task descriptions, run notes,
  agent names) is never read for any figure; injection text in a note changes
  nothing. Only numeric / status fields feed the math.
- Nothing here touches a database, a network, or the filesystem, and nothing
  here approves, pays, or changes a permission. This module only reports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "RunRecord",
    "CostFigures",
    "BudgetCheck",
    "MAX_RECORDS",
    "TERMINAL_STATUSES",
    "validate_record",
    "validate_records",
    "summarize",
    "summarize_by_agent",
    "summarize_by_scope",
    "rollup",
    "spend_vs_budget",
    "figures_fingerprint",
]

MAX_RECORDS = 50_000
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ALL_STATUSES = frozenset(
    {"queued", "running", "awaiting_approval", "completed", "failed", "cancelled"}
)
_MONEY_Q = Decimal("0.0001")
_RATE_Q = Decimal("0.01")
_WARN_PCT = Decimal("80")


def _is_money(value: Any) -> bool:
    """True for exact money values: Decimal, or int (never bool, never float)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (Decimal, int))


def _money(value: Any, field: str) -> Decimal:
    if not _is_money(value):
        raise ValueError(f"{field} must be a Decimal (never float or str)")
    result = Decimal(value)
    if result < 0:
        raise ValueError(f"{field} must not be negative")
    return result


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


@dataclass(frozen=True)
class RunRecord:
    """One recorded run, as read from a ledger (never a model claim).

    ``estimated_cost_usd`` is the recorded estimate for the run, or ``None``
    when no rate was configured for its provider:model. ``human_minutes`` and
    ``human_hourly_rate_usd`` are the company-recorded human effort for the
    same task; either may be ``None``. ``notes`` is untrusted text and is
    never read by any figure in this module.
    """

    id: str
    agent_id: str
    scope_id: str
    status: str
    estimated_cost_usd: Decimal | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    human_minutes: Decimal | None = None
    human_hourly_rate_usd: Decimal | None = None
    notes: str = ""


@dataclass(frozen=True)
class CostFigures:
    """Cost/ROI figures for one agent, one scope, or the whole org."""

    runs: int
    terminal: int
    completed: int
    failed: int
    cancelled: int
    success_rate: Decimal | None  # percent, 2dp; None when no terminal runs
    failure_rate: Decimal | None  # percent, 2dp; None when no terminal runs
    total_cost: Decimal  # sum of known run costs only, 4dp
    cost_known_count: int  # runs with a recorded cost
    cost_unknown_count: int  # runs with no configured rate (never counted as 0)
    cost_per_task: Decimal | None  # total_cost / cost_known_count, 4dp
    cost_per_successful_task: Decimal | None  # total_cost / completed, 4dp
    prompt_tokens: int
    completion_tokens: int
    human_cost: Decimal | None  # None unless every run has minutes + rate
    human_cost_status: str  # "known" | "partial" | "unknown"
    roi: Decimal | None  # (human - ai) / ai, 4dp; None unless fully known
    comparison: str  # "ai_cheaper" | "human_cheaper" | "equal" | "unknown"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe rendering: Decimals become strings, None stays None."""
        out: dict[str, Any] = {}
        for name in (
            "runs",
            "terminal",
            "completed",
            "failed",
            "cancelled",
            "cost_known_count",
            "cost_unknown_count",
            "prompt_tokens",
            "completion_tokens",
            "human_cost_status",
            "comparison",
        ):
            out[name] = getattr(self, name)
        for name in (
            "success_rate",
            "failure_rate",
            "total_cost",
            "cost_per_task",
            "cost_per_successful_task",
            "human_cost",
            "roi",
        ):
            value = getattr(self, name)
            out[name] = None if value is None else str(value)
        return out


@dataclass(frozen=True)
class BudgetCheck:
    """Spend vs a budget. ``status`` is "no_budget" when no budget is set."""

    used: Decimal
    budget: Decimal | None
    remaining: Decimal | None
    used_pct: Decimal | None  # 2dp; None when no budget
    status: str  # "ok" | "warn" | "over" | "no_budget"

    def as_dict(self) -> dict[str, Any]:
        return {
            "used": str(self.used),
            "budget": None if self.budget is None else str(self.budget),
            "remaining": None if self.remaining is None else str(self.remaining),
            "used_pct": None if self.used_pct is None else str(self.used_pct),
            "status": self.status,
        }


def validate_record(record: Any) -> RunRecord:
    """Validate one record; returns it unchanged. Raises ValueError."""
    if not isinstance(record, RunRecord):
        raise ValueError("record must be a RunRecord")
    for field in ("id", "agent_id", "scope_id"):
        value = getattr(record, field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty str")
    if record.status not in ALL_STATUSES:
        raise ValueError(f"unknown status: {record.status!r}")
    if record.estimated_cost_usd is not None:
        _money(record.estimated_cost_usd, "estimated_cost_usd")
    _count(record.prompt_tokens, "prompt_tokens")
    _count(record.completion_tokens, "completion_tokens")
    if record.human_minutes is not None:
        _money(record.human_minutes, "human_minutes")
    if record.human_hourly_rate_usd is not None:
        _money(record.human_hourly_rate_usd, "human_hourly_rate_usd")
    if not isinstance(record.notes, str):
        raise ValueError("notes must be a str")
    return record


def validate_records(records: Any) -> list[RunRecord]:
    """Validate a batch; returns a list. Raises ValueError."""
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise ValueError("records must be a list of RunRecord")
    items = list(records)
    if len(items) > MAX_RECORDS:
        raise ValueError(f"at most {MAX_RECORDS} records")
    seen: set[str] = set()
    for record in items:
        validate_record(record)
        if record.id in seen:
            raise ValueError(f"duplicate record id: {record.id!r}")
        seen.add(record.id)
    return items


def _pct(part: int, whole: int) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) * Decimal(100) / Decimal(whole)).quantize(_RATE_Q)


def summarize(records: Sequence[RunRecord]) -> CostFigures:
    """Figures for one group of records (one agent, one scope, or all)."""
    items = validate_records(records)
    terminal = sum(1 for r in items if r.status in TERMINAL_STATUSES)
    completed = sum(1 for r in items if r.status == "completed")
    failed = sum(1 for r in items if r.status == "failed")
    cancelled = sum(1 for r in items if r.status == "cancelled")
    known = [r for r in items if r.estimated_cost_usd is not None]
    total_cost = sum(
        (Decimal(r.estimated_cost_usd) for r in known), Decimal("0")
    ).quantize(_MONEY_Q)
    cost_per_task = (
        (total_cost / Decimal(len(known))).quantize(_MONEY_Q) if known else None
    )
    cost_per_successful_task = (
        (total_cost / Decimal(completed)).quantize(_MONEY_Q)
        if completed and known
        else None
    )
    human_known = [
        r
        for r in items
        if r.human_minutes is not None and r.human_hourly_rate_usd is not None
    ]
    if not items:
        human_status = "unknown"
    elif len(human_known) == len(items):
        human_status = "known"
    elif human_known:
        human_status = "partial"
    else:
        human_status = "unknown"
    human_cost: Decimal | None = None
    if human_status == "known":
        human_cost = sum(
            (
                Decimal(r.human_minutes)
                * Decimal(r.human_hourly_rate_usd)
                / Decimal(60)
                for r in human_known
            ),
            Decimal("0"),
        ).quantize(_MONEY_Q)
    roi: Decimal | None = None
    comparison = "unknown"
    if human_cost is not None:
        if total_cost > 0:
            roi = ((human_cost - total_cost) / total_cost).quantize(_MONEY_Q)
            if human_cost > total_cost:
                comparison = "ai_cheaper"
            elif human_cost < total_cost:
                comparison = "human_cheaper"
            else:
                comparison = "equal"
        elif human_cost == 0 and total_cost == 0:
            comparison = "equal"
    return CostFigures(
        runs=len(items),
        terminal=terminal,
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        success_rate=_pct(completed, terminal),
        failure_rate=_pct(failed, terminal),
        total_cost=total_cost,
        cost_known_count=len(known),
        cost_unknown_count=len(items) - len(known),
        cost_per_task=cost_per_task,
        cost_per_successful_task=cost_per_successful_task,
        prompt_tokens=sum(r.prompt_tokens for r in items),
        completion_tokens=sum(r.completion_tokens for r in items),
        human_cost=human_cost,
        human_cost_status=human_status,
        roi=roi,
        comparison=comparison,
    )


def _grouped(
    records: Sequence[RunRecord], key: str
) -> dict[str, CostFigures]:
    items = validate_records(records)
    groups: dict[str, list[RunRecord]] = {}
    for record in items:
        groups.setdefault(getattr(record, key), []).append(record)
    return {name: summarize(rows) for name, rows in sorted(groups.items())}


def summarize_by_agent(records: Sequence[RunRecord]) -> dict[str, CostFigures]:
    """Per-agent figures, keyed by agent id (sorted)."""
    return _grouped(records, "agent_id")


def summarize_by_scope(records: Sequence[RunRecord]) -> dict[str, CostFigures]:
    """Per-scope figures, keyed by scope id (sorted). Never mixes scopes."""
    return _grouped(records, "scope_id")


def rollup(records: Sequence[RunRecord]) -> dict[str, Any]:
    """Org-wide figures plus per-agent and per-scope breakdowns."""
    items = validate_records(records)
    return {
        "overall": summarize(items),
        "by_agent": summarize_by_agent(items),
        "by_scope": summarize_by_scope(items),
    }


def spend_vs_budget(
    total_cost: Any, budget: Any | None
) -> BudgetCheck:
    """Spend vs a budget. Warn at >=80% used, over when used exceeds budget."""
    used = _money(total_cost, "total_cost")
    if budget is None:
        return BudgetCheck(
            used=used.quantize(_MONEY_Q),
            budget=None,
            remaining=None,
            used_pct=None,
            status="no_budget",
        )
    limit = _money(budget, "budget")
    if limit == 0:
        raise ValueError("budget must not be zero (use None for no budget)")
    remaining = (limit - used).quantize(_MONEY_Q)
    used_pct = (used * Decimal(100) / limit).quantize(_RATE_Q)
    status = "over" if used > limit else ("warn" if used_pct >= _WARN_PCT else "ok")
    return BudgetCheck(
        used=used.quantize(_MONEY_Q),
        budget=limit.quantize(_MONEY_Q),
        remaining=remaining,
        used_pct=used_pct,
        status=status,
    )


def _record_json(record: RunRecord) -> Mapping[str, Any]:
    # Notes are deliberately excluded: untrusted text never feeds a figure
    # and must not change the fingerprint.
    return {
        "id": record.id,
        "agent_id": record.agent_id,
        "scope_id": record.scope_id,
        "status": record.status,
        "cost": None
        if record.estimated_cost_usd is None
        else str(Decimal(record.estimated_cost_usd)),
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "human_minutes": None
        if record.human_minutes is None
        else str(Decimal(record.human_minutes)),
        "human_hourly_rate_usd": None
        if record.human_hourly_rate_usd is None
        else str(Decimal(record.human_hourly_rate_usd)),
    }


def figures_fingerprint(records: Sequence[RunRecord]) -> str:
    """Deterministic SHA-256 over the records (list-order independent)."""
    items = validate_records(records)
    payload = [_record_json(r) for r in sorted(items, key=lambda r: r.id)]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
