"""Step T slices 2-3: routed client wiring, fallback chain, chat metadata.

DB-backed: providers are seeded through the admin API (configure / stage /
activate) and the fake DeepSeek adapter answers without any network call.
No prompt text, key or endpoint is asserted anywhere here -- routing
decisions carry ids and cost estimates only.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.errors import LLMNotConfiguredError, LLMUnavailableError, RoutingBudgetExceededError
from app.db.session import test_engine_options as _engine_options
from app.schemas.chat import ChatResponse
from app.services.llm_runtime import LLMRuntime, resolve_chat_client
from app.services.model_router import RoutingPolicy, TaskClass
from tests.conftest import TEST_DATABASE_URL, VALID_KEY_1, run_db
from tests.test_providers import activate, configure, stage

D = Decimal

PRICING = {
    "deepseek:deepseek-flash": {"input_per_1m_tokens_usd": 0.10, "output_per_1m_tokens_usd": 0.40},
    "deepseek:deepseek-v4-pro": {"input_per_1m_tokens_usd": 2.00, "output_per_1m_tokens_usd": 8.00},
}


def _runtime(*, pricing=None):
    settings = Settings(llm_model_pricing_json=pricing or {})
    cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    return LLMRuntime(settings, cipher)


def _run(coro_fn):
    """Run ``await coro_fn(session)`` on a fresh session to the test DB."""

    async def _go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_engine_options())
        try:
            async with AsyncSession(engine) as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed(admin_client):
    configure(admin_client)
    assert stage(admin_client, VALID_KEY_1).status_code == 200
    assert activate(admin_client).status_code == 200


# ------------------------------------------------------------ slice 2: routing


def test_routed_client_picks_cheapest_for_simple(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        routed = await runtime.get_routed_client(session, "hi")
        reply = await routed.client.chat("hi")
        return routed, reply

    routed, reply = _run(_go)
    assert routed.decision.task_class is TaskClass.SIMPLE
    assert (routed.decision.provider, routed.decision.model) == ("deepseek", "deepseek-flash")
    assert [c.key for c in routed.decision.alternates] == ["deepseek:deepseek-v4-pro"]
    assert routed.decision.estimated_cost_usd is not None
    assert reply.model == "deepseek-flash" and reply.provider == "deepseek"


def test_routed_client_balanced_complex_picks_pro(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(
            session, "analyse this invoice step by step",
            task_class=TaskClass.COMPLEX, policy=RoutingPolicy.BALANCED,
        )

    routed = _run(_go)
    assert (routed.decision.provider, routed.decision.model) == ("deepseek", "deepseek-v4-pro")
    assert routed.decision.policy is RoutingPolicy.BALANCED


def test_routed_client_cheapest_complex_stays_cheap(admin_client):
    # Cheapest policy ranks by cost only (capability filtering still applies);
    # the flagship is only forced by the balanced policy.
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(
            session, "analyse this invoice step by step", task_class=TaskClass.COMPLEX
        )

    routed = _run(_go)
    assert routed.decision.model == "deepseek-flash"


def test_explicit_user_pick_bypasses_routing(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        client, decision = await resolve_chat_client(
            runtime, session, "hi", model_choice=("deepseek", "deepseek-v4-pro")
        )
        return client, decision, await client.chat("hi")

    client, decision, reply = _run(_go)
    assert decision is None
    assert reply.model == "deepseek-v4-pro"


def test_unrouted_client_behaves_as_before(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        client = await runtime.get_client(session)
        return client, await client.chat("hi")

    client, reply = _run(_go)
    from app.services.llm_fallback import FallbackLLM

    assert not isinstance(client, FallbackLLM)  # no fallback configured
    assert reply.model == "deepseek-flash"


def test_routed_client_without_provider_is_not_configured(admin_client):
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(session, "hi")

    with pytest.raises(LLMNotConfiguredError):
        _run(_go)


def test_grounded_research_with_only_deepseek_is_unavailable(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(session, "latest market news", needs_research=True)

    with pytest.raises(LLMUnavailableError):
        _run(_go)


def test_fallback_chain_honoured_on_primary_failure(admin_client, monkeypatch):
    from tests.conftest import FakeProviderClient

    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def flaky_chat(self, message, *, system_prompt=None):
        FakeProviderClient.calls.append((system_prompt, message))
        if self.params.model == "deepseek-flash":
            raise LLMUnavailableError("primary down")
        return ChatResponse(reply="from pro", model=self.params.model, provider="deepseek")

    monkeypatch.setattr(FakeProviderClient, "chat", flaky_chat)

    async def _go(session):
        routed = await runtime.get_routed_client(session, "hi")
        return routed, await routed.client.chat("hi")

    routed, reply = _run(_go)
    assert routed.decision.model == "deepseek-flash"  # the router still picks flash first
    assert reply.reply == "from pro" and reply.model == "deepseek-v4-pro"
    assert len(FakeProviderClient.calls) == 2  # flash attempted, then pro


# ------------------------------------------------------------ slice 3: budget guard


def test_budget_refuses_complex_over_budget(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(
            session, "analyse this invoice step by step",
            task_class=TaskClass.COMPLEX, monthly_budget_usd=D("0.00000001"),
        )

    with pytest.raises(RoutingBudgetExceededError) as excinfo:
        _run(_go)
    assert excinfo.value.code == "routing_budget_exceeded"
    assert excinfo.value.details["task_class"] == "complex"


def test_budget_downgrades_simple_over_budget(admin_client):
    _seed(admin_client)
    runtime = _runtime(pricing=PRICING)

    async def _go(session):
        return await runtime.get_routed_client(session, "hi", monthly_budget_usd=D("0.00000001"))

    routed = _run(_go)
    assert routed.decision.budget_action == "downgrade"
    assert routed.decision.model == "deepseek-flash"  # already the cheapest capable model
    assert "downgraded" in routed.decision.reason


def test_budget_refuses_unpriced_model(admin_client):
    # No pricing configured: the cost is unknown, and unknown is never free.
    _seed(admin_client)
    runtime = _runtime()

    async def _go(session):
        return await runtime.get_routed_client(session, "hi", monthly_budget_usd=D("10"))

    with pytest.raises(RoutingBudgetExceededError):
        _run(_go)


# ------------------------------------------------------------ slice 2: HTTP metadata


def test_chat_endpoint_exposes_routing_metadata(admin_client):
    _seed(admin_client)
    response = admin_client.post("/api/ai/chat", json={"message": "hello"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "deepseek-flash"
    routing = body["routing"]
    assert routing["provider"] == "deepseek"
    assert routing["model"] == "deepseek-flash"
    assert routing["task_class"] == "simple"
    assert routing["policy"] == "cheapest"
    assert routing["alternates"] == ["deepseek:deepseek-v4-pro"]
    assert routing["budget_action"] == "allow"
    assert "deepseek-flash" in routing["reason"]
    # The routing reason never carries prompt text.
    assert "hello" not in routing["reason"]
    rows = run_db(lambda c: c.execute(text("SELECT COUNT(*) FROM model_routing_records"))).scalar_one()
    assert rows == 1


def test_chat_without_provider_still_503(admin_client):
    response = admin_client.post("/api/ai/chat", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"
