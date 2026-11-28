"""Step T (slice 1): smart model router — pure-function tests.

No network, no model calls, no secrets. The router only receives candidate
metadata and a pricing table, so these tests prove classification, filtering,
ranking, the fallback chain and exact cost math. (The repo's conftest still
prepares the throwaway *_test database for the session; these tests never
touch it.)
"""

from decimal import Decimal

import pytest

from app.services.model_router import (
    ModelCandidate,
    NoCapableModelError,
    RoutingDecision,
    RoutingPolicy,
    TaskClass,
    classify_task,
    estimate_cost_usd,
    route,
)

D = Decimal


def _flash() -> ModelCandidate:
    return ModelCandidate(
        provider="deepseek",
        model="deepseek-flash",
        context_window=1_000_000,
        max_output_tokens=384_000,
        vision=True,
        grounded_research=False,
        capability_tier=1,
        latency_tier=1,
        input_per_1m_tokens_usd=D("0.10"),
        output_per_1m_tokens_usd=D("0.40"),
    )


def _pro() -> ModelCandidate:
    return ModelCandidate(
        provider="deepseek",
        model="deepseek-v4-pro",
        context_window=1_000_000,
        max_output_tokens=384_000,
        vision=False,
        grounded_research=False,
        capability_tier=3,
        latency_tier=3,
        input_per_1m_tokens_usd=D("2.00"),
        output_per_1m_tokens_usd=D("8.00"),
    )


def _local() -> ModelCandidate:
    # Local Ollama-style endpoint: zero cloud cost, slower, no vision.
    return ModelCandidate(
        provider="ollama",
        model="local-small",
        context_window=32_768,
        max_output_tokens=4_096,
        vision=False,
        grounded_research=False,
        capability_tier=1,
        latency_tier=3,
        input_per_1m_tokens_usd=D("0"),
        output_per_1m_tokens_usd=D("0"),
    )


def _unpriced() -> ModelCandidate:
    return ModelCandidate(
        provider="gemini",
        model="gemini-flash-lite-latest",
        context_window=1_000_000,
        max_output_tokens=65_536,
        vision=True,
        grounded_research=True,
        capability_tier=1,
        latency_tier=1,
    )


# --- classification -------------------------------------------------------


def test_classify_short_greeting_is_simple():
    assert classify_task("hi") is TaskClass.SIMPLE


def test_classify_short_question_is_simple():
    assert classify_task("What time is it?") is TaskClass.SIMPLE


def test_classify_document_question_is_standard():
    assert classify_task("What does the report say about last month's sales?") is TaskClass.STANDARD


def test_classify_contract_question_is_complex():
    assert classify_task("What does the contract say about payment terms?") is TaskClass.COMPLEX


def test_classify_analysis_keyword_is_complex():
    assert classify_task("Analyse this invoice and verify every line") is TaskClass.COMPLEX


def test_classify_long_prompt_is_complex():
    assert classify_task("x" * 5000) is TaskClass.COMPLEX


def test_classify_live_question_is_grounded_research():
    assert classify_task("What is the latest cricket score today?") is TaskClass.GROUNDED_RESEARCH


def test_classify_vision_hint_wins():
    assert classify_task("describe this", needs_vision=True) is TaskClass.VISION


def test_classify_research_hint_wins():
    assert classify_task("summarise this", needs_research=True) is TaskClass.GROUNDED_RESEARCH


def test_classify_explicit_class_overrides_heuristic():
    assert classify_task("hi", task_class=TaskClass.COMPLEX) is TaskClass.COMPLEX
    assert classify_task("analyse everything", task_class=TaskClass.SIMPLE) is TaskClass.SIMPLE


def test_classify_empty_prompt_is_simple():
    assert classify_task("") is TaskClass.SIMPLE


# --- cost estimation ------------------------------------------------------


def test_estimate_cost_exact_decimal_math():
    cost = estimate_cost_usd(_flash(), 1_000_000, 500_000)
    assert cost == D("0.30000000")  # 1.0M * 0.10 + 0.5M * 0.40, per 1M tokens


def test_estimate_cost_zero_tokens_is_zero():
    assert estimate_cost_usd(_flash(), 0, 0) == D("0.00000000")


def test_estimate_cost_unpriced_model_is_none():
    assert estimate_cost_usd(_unpriced(), 1000, 1000) is None


def test_estimate_cost_rejects_negative_tokens():
    with pytest.raises(ValueError):
        estimate_cost_usd(_flash(), -1, 0)


# --- routing --------------------------------------------------------------


def test_route_cheapest_picks_flash_for_standard_task():
    decision = route(TaskClass.STANDARD, [_pro(), _flash()], estimated_prompt_tokens=1000, estimated_completion_tokens=500)
    assert (decision.provider, decision.model) == ("deepseek", "deepseek-flash")
    assert decision.estimated_cost_usd == D("0.00030000")
    assert decision.alternates[0].model == "deepseek-v4-pro"
    assert isinstance(decision, RoutingDecision)


def test_route_cheapest_prefers_free_local_model():
    decision = route(
        TaskClass.SIMPLE, [_flash(), _local()],
        estimated_prompt_tokens=1000, estimated_completion_tokens=500,
    )
    assert decision.model == "local-small"
    assert decision.estimated_cost_usd == D("0.00000000")


def test_route_unpriced_models_sort_after_priced_ones():
    # Unpriced is never silently treated as free.
    decision = route(TaskClass.STANDARD, [_unpriced(), _flash()])
    assert decision.model == "deepseek-flash"
    assert decision.alternates[0].model == "gemini-flash-lite-latest"


def test_route_vision_task_filters_out_non_vision_models():
    decision = route(TaskClass.VISION, [_pro(), _flash(), _unpriced()])
    assert decision.model == "deepseek-flash"
    assert [c.model for c in decision.alternates] == ["gemini-flash-lite-latest"]


def test_route_vision_task_with_no_vision_model_fails():
    with pytest.raises(NoCapableModelError):
        route(TaskClass.VISION, [_pro(), _local()])


def test_route_research_task_requires_grounded_model():
    decision = route(TaskClass.GROUNDED_RESEARCH, [_flash(), _unpriced()])
    assert decision.model == "gemini-flash-lite-latest"


def test_route_context_window_filters_oversized_prompts():
    with pytest.raises(NoCapableModelError):
        route(TaskClass.STANDARD, [_local()], estimated_prompt_tokens=100_000)


def test_route_output_limit_filters_oversized_completions():
    with pytest.raises(NoCapableModelError):
        route(TaskClass.STANDARD, [_local()], estimated_completion_tokens=10_000)


def test_route_fastest_picks_lowest_latency_tier():
    decision = route(
        TaskClass.STANDARD, [_local(), _flash(), _pro()], policy=RoutingPolicy.FASTEST
    )
    assert decision.model == "deepseek-flash"  # latency tier 1 beats tier 3


def test_route_balanced_picks_cheapest_model_meeting_complex_tier():
    decision = route(
        TaskClass.COMPLEX, [_flash(), _pro()], policy=RoutingPolicy.BALANCED
    )
    assert decision.model == "deepseek-v4-pro"
    assert "capability tier" in decision.reason


def test_route_budget_refuses_over_budget_choices():
    with pytest.raises(NoCapableModelError):
        route(
            TaskClass.STANDARD, [_pro()],
            estimated_prompt_tokens=1_000_000, estimated_completion_tokens=500_000,
            max_cost_usd=D("0.50"),
        )


def test_route_budget_refuses_unpriced_models():
    # An unpriced model cannot prove it fits the budget.
    with pytest.raises(NoCapableModelError):
        route(TaskClass.STANDARD, [_unpriced()], max_cost_usd=D("10"))


def test_route_empty_candidates_fails():
    with pytest.raises(NoCapableModelError):
        route(TaskClass.SIMPLE, [])


def test_route_negative_token_estimate_fails():
    with pytest.raises(ValueError):
        route(TaskClass.SIMPLE, [_flash()], estimated_prompt_tokens=-5)


def test_route_decision_reason_names_model_and_cost():
    decision = route(TaskClass.SIMPLE, [_flash(), _pro()])
    assert "deepseek:deepseek-flash" in decision.reason
    assert "1 fallback(s) available" in decision.reason


def test_route_decision_contains_no_prompt_text():
    decision = route(TaskClass.STANDARD, [_flash()])
    assert "secret" not in decision.reason
    assert decision.estimated_prompt_tokens == 0
