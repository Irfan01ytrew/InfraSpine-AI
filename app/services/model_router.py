"""Smart model router (Step T, slice 1): pick the cheapest/fastest capable model
for a task, with an ordered fallback chain and a cost estimate.

Pure functions only: no I/O, no database, no network, no model calls, no
secrets. Callers supply the enabled candidate models (from the provider
catalog + admin config) and the pricing table (``Settings.llm_model_pricing_json``,
``"provider:model"`` -> per-1M-token USD rates). Routing never changes what a
user may see or do -- it only picks which model answers.

Pricing follows the existing convention in ``app/agent_studio/service.py``:
cost = (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000,
computed with ``Decimal``. A model without a rate is never treated as free:
its cost is unknown and it sorts after every priced model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "TaskClass",
    "RoutingPolicy",
    "ModelCandidate",
    "RoutingDecision",
    "NoCapableModelError",
    "BudgetAction",
    "BudgetDecision",
    "classify_task",
    "estimate_cost_usd",
    "route",
    "check_budget",
]


class TaskClass(StrEnum):
    """What kind of work the model is being asked to do."""

    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"
    VISION = "vision"
    GROUNDED_RESEARCH = "grounded_research"


class RoutingPolicy(StrEnum):
    """How the router ranks the models that can do the job."""

    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    BALANCED = "balanced"


class NoCapableModelError(ValueError):
    """No enabled candidate can do this task (capability, context or budget)."""


class BudgetAction(StrEnum):
    """What the budget guard decided for a routed task."""

    ALLOW = "allow"
    DOWNGRADE = "downgrade"
    REFUSE = "refuse"


@dataclass(frozen=True)
class BudgetDecision:
    """The budget guard's answer. Ids and amounts only -- never prompt text."""

    action: BudgetAction
    reason: str
    spent_usd: Decimal
    estimated_cost_usd: Decimal | None
    monthly_budget_usd: Decimal | None


def check_budget(
    task_class: TaskClass,
    *,
    spent_usd: Decimal,
    estimated_cost_usd: Decimal | None,
    monthly_budget_usd: Decimal | None,
) -> BudgetDecision:
    """Decide whether a routed task may run under a monthly budget.

    No budget configured: always allow. An unpriced model can never prove
    it fits, so it is refused under a budget (never treated as free).
    A task that fits runs as routed. A task over budget is refused when it
    is ``complex`` (downgrading it would risk a wrong answer on hard work)
    and downgraded to the cheapest capable model otherwise."""
    if monthly_budget_usd is None:
        return BudgetDecision(
            BudgetAction.ALLOW, "No monthly budget is configured.", spent_usd, estimated_cost_usd, None
        )
    if estimated_cost_usd is None:
        return BudgetDecision(
            BudgetAction.REFUSE,
            "Cost is unknown (unpriced model); unpriced models are never treated as free under a budget.",
            spent_usd,
            None,
            monthly_budget_usd,
        )
    if spent_usd + estimated_cost_usd <= monthly_budget_usd:
        return BudgetDecision(
            BudgetAction.ALLOW,
            f"Estimated ${estimated_cost_usd} fits the remaining monthly budget.",
            spent_usd,
            estimated_cost_usd,
            monthly_budget_usd,
        )
    if task_class is TaskClass.COMPLEX:
        return BudgetDecision(
            BudgetAction.REFUSE,
            f"A {task_class.value} task would exceed the monthly budget of "
            f"${monthly_budget_usd} (already spent ${spent_usd}).",
            spent_usd,
            estimated_cost_usd,
            monthly_budget_usd,
        )
    return BudgetDecision(
        BudgetAction.DOWNGRADE,
        f"Estimated ${estimated_cost_usd} would exceed the monthly budget of "
        f"${monthly_budget_usd} (already spent ${spent_usd}); "
        "downgraded to the cheapest capable model.",
        spent_usd,
        estimated_cost_usd,
        monthly_budget_usd,
    )


# Heuristic signals for classification. The caller always wins: an explicit
# ``task_class`` overrides every signal below.
_RESEARCH_SIGNALS = (
    "latest",
    "live",
    "current",
    "today",
    "news",
    "price",
    "score",
    "market",
    "weather",
    "search the web",
    "on the internet",
)
_COMPLEX_SIGNALS = (
    "analyse",
    "analyze",
    "compare",
    "verify",
    "audit",
    "forecast",
    "reason",
    "step by step",
    "multi-step",
    "invoice",
    "contract",
    "reconcile",
)
_SIMPLE_SIGNALS = ("hi", "hello", "hey", "thanks", "thank you", "ok", "yes", "no")


def classify_task(
    prompt: str,
    *,
    task_class: TaskClass | None = None,
    needs_vision: bool = False,
    needs_research: bool = False,
    estimated_prompt_tokens: int = 0,
) -> TaskClass:
    """Classify a task. Pure heuristic; an explicit ``task_class`` wins.

    Order matters: vision and research are capability requirements, so they
    outrank the complexity signals. A very long prompt is complex work even
    without trigger words.
    """
    if task_class is not None:
        return task_class
    if needs_vision:
        return TaskClass.VISION
    if needs_research:
        return TaskClass.GROUNDED_RESEARCH
    text = (prompt or "").strip().lower()
    if not text:
        return TaskClass.SIMPLE
    if any(signal in text for signal in _RESEARCH_SIGNALS):
        return TaskClass.GROUNDED_RESEARCH
    if len(text) > 4000 or estimated_prompt_tokens > 8000:
        return TaskClass.COMPLEX
    if any(signal in text for signal in _COMPLEX_SIGNALS):
        return TaskClass.COMPLEX
    if len(text) <= 40 and any(signal in text for signal in _SIMPLE_SIGNALS):
        return TaskClass.SIMPLE
    if len(text) <= 20:
        return TaskClass.SIMPLE
    return TaskClass.STANDARD


@dataclass(frozen=True)
class ModelCandidate:
    """One model the router may pick. Supplied by the caller, never read from
    credentials, the database or the network by this module."""

    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    vision: bool = False
    grounded_research: bool = False
    # 1 = fast/cheap, 2 = standard, 3 = flagship reasoning. The provider
    # catalog has no reasoning tier today, so the caller supplies it.
    capability_tier: int = 2
    # 1 = fastest, 3 = slowest; used only by the FASTEST policy.
    latency_tier: int = 2
    input_per_1m_tokens_usd: Decimal | None = None
    output_per_1m_tokens_usd: Decimal | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class RoutingDecision:
    """The router's answer. Contains ids, token estimates and a cost estimate
    only -- never prompt text, keys or endpoints."""

    task_class: TaskClass
    provider: str
    model: str
    alternates: tuple[ModelCandidate, ...] = ()
    estimated_cost_usd: Decimal | None = None
    reason: str = ""
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    # Policy that produced this ranking, and what the budget guard did with it
    # ("allow" normally; "downgrade" when an over-budget task was re-routed to
    # the cheapest capable model). Slice 3 sets budget_action.
    policy: RoutingPolicy = RoutingPolicy.CHEAPEST
    budget_action: str = "allow"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def estimate_cost_usd(
    candidate: ModelCandidate,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal | None:
    """Exact cost estimate, or ``None`` when either rate is missing."""
    if candidate.input_per_1m_tokens_usd is None or candidate.output_per_1m_tokens_usd is None:
        return None
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("Token counts must not be negative.")
    total = (
        Decimal(max(0, prompt_tokens)) * candidate.input_per_1m_tokens_usd
        + Decimal(max(0, completion_tokens)) * candidate.output_per_1m_tokens_usd
    ) / Decimal(1_000_000)
    return total.quantize(Decimal("0.00000001"))


def _capable(
    candidate: ModelCandidate,
    task_class: TaskClass,
    estimated_prompt_tokens: int,
    estimated_completion_tokens: int,
) -> bool:
    if task_class is TaskClass.VISION and not candidate.vision:
        return False
    if task_class is TaskClass.GROUNDED_RESEARCH and not candidate.grounded_research:
        return False
    if estimated_prompt_tokens > candidate.context_window:
        return False
    if estimated_completion_tokens > candidate.max_output_tokens:
        return False
    return True


def _required_tier(task_class: TaskClass) -> int:
    """Minimum capability tier for a task class (BALANCED policy)."""
    return {
        TaskClass.SIMPLE: 1,
        TaskClass.STANDARD: 1,
        TaskClass.COMPLEX: 3,
        TaskClass.VISION: 1,
        TaskClass.GROUNDED_RESEARCH: 2,
    }[task_class]


def route(
    task_class: TaskClass,
    candidates: list[ModelCandidate],
    *,
    estimated_prompt_tokens: int = 0,
    estimated_completion_tokens: int = 0,
    policy: RoutingPolicy = RoutingPolicy.CHEAPEST,
    max_cost_usd: Decimal | None = None,
) -> RoutingDecision:
    """Pick the best model for ``task_class`` from ``candidates``.

    Filters out models that cannot do the job, ranks the survivors by
    ``policy``, and returns the winner plus the remaining survivors in rank
    order as the fallback chain. ``max_cost_usd`` refuses any choice whose
    estimated cost exceeds the budget (unpriced models cannot prove they fit,
    so they are refused too when a budget is set).
    """
    if not candidates:
        raise NoCapableModelError("No model candidates were provided.")
    if estimated_prompt_tokens < 0 or estimated_completion_tokens < 0:
        raise ValueError("Token estimates must not be negative.")

    capable = [
        c
        for c in candidates
        if _capable(c, task_class, estimated_prompt_tokens, estimated_completion_tokens)
    ]
    if policy is RoutingPolicy.BALANCED:
        required = _required_tier(task_class)
        tiered = [c for c in capable if c.capability_tier >= required]
        # Never leave the caller with nothing when a lower tier could still try.
        if tiered:
            capable = tiered
    if not capable:
        raise NoCapableModelError(
            f"No enabled model can handle a {task_class.value} task "
            f"(prompt ~{estimated_prompt_tokens} tokens, "
            f"completion ~{estimated_completion_tokens} tokens)."
        )

    def cost_of(candidate: ModelCandidate) -> Decimal | None:
        return estimate_cost_usd(candidate, estimated_prompt_tokens, estimated_completion_tokens)

    def rank_key(candidate: ModelCandidate) -> tuple:
        cost = cost_of(candidate)
        # Unpriced models sort after every priced model: unknown cost is never
        # treated as free.
        cost_rank = (1, Decimal("0")) if cost is None else (0, cost)
        if policy is RoutingPolicy.FASTEST:
            return (candidate.latency_tier, cost_rank, candidate.capability_tier)
        if policy is RoutingPolicy.BALANCED:
            return (cost_rank, candidate.latency_tier)
        return (cost_rank, candidate.latency_tier, -candidate.capability_tier)

    ranked = sorted(capable, key=rank_key)
    if max_cost_usd is not None:
        affordable = [
            c for c in ranked if (cost_of(c) is not None and cost_of(c) <= max_cost_usd)
        ]
        if not affordable:
            raise NoCapableModelError(
                f"No enabled model can handle a {task_class.value} task within "
                f"a budget of ${max_cost_usd}."
            )
        ranked = affordable

    chosen = ranked[0]
    cost = cost_of(chosen)
    reasons = {
        RoutingPolicy.CHEAPEST: "lowest estimated cost of the capable models",
        RoutingPolicy.FASTEST: "lowest latency tier of the capable models",
        RoutingPolicy.BALANCED: "cheapest model meeting the task's capability tier",
    }
    return RoutingDecision(
        task_class=task_class,
        provider=chosen.provider,
        model=chosen.model,
        alternates=tuple(ranked[1:]),
        estimated_cost_usd=cost,
        reason=(
            f"Chose {chosen.key} for a {task_class.value} task: "
            f"{reasons[policy]}"
            + (f" (estimated ${cost})" if cost is not None else " (cost unknown: unpriced model)")
            + (f"; {len(ranked) - 1} fallback(s) available" if len(ranked) > 1 else "; no fallback available")
        ),
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_completion_tokens=estimated_completion_tokens,
        policy=policy,
    )
