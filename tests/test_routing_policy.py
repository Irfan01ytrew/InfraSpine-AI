"""Step T slice 4: stored routing policy, budget wiring, admin API.

Pure validation/effective helpers are tested without a DB; the admin API
and the budget wiring run against the throwaway *_test database with
providers seeded through the admin API (fake DeepSeek adapter, no network).
No prompt text, key or endpoint is asserted anywhere here.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.errors import RoutingBudgetExceededError
from app.services.model_router import RoutingPolicy, TaskClass
from app.services.routing_policy import (
    DEFAULT_CONFIG,
    department_for_user,
    effective_budget_usd,
    effective_capability_tier,
    effective_policy,
    parse_routing_policy,
    routing_context_for_user,
    save_routing_policy,
    validate_routing_policy,
)
from tests.conftest import ADMIN_EMAIL, run_db
from tests.test_model_router_wiring import PRICING, _run, _runtime, _seed
from tests.test_providers import configure

D = Decimal

FULL_POLICY = {
    "default_policy": "cheapest",
    "task_class_policies": {"complex": "balanced", "vision": "fastest"},
    "capability_tiers": {"deepseek:deepseek-v4-pro": 3},
    "monthly_budget_usd": "25.00",
    "department_budgets": {},
}


# ------------------------------------------------------------ pure: validate


def test_validate_accepts_empty_and_full_policy():
    assert validate_routing_policy({}) == []
    assert validate_routing_policy(FULL_POLICY) == []


def test_validate_rejects_bad_policy_and_task_class():
    problems = validate_routing_policy({"default_policy": "random"})
    assert any("default_policy" in p for p in problems)
    problems = validate_routing_policy({"task_class_policies": {"weird": "cheapest"}})
    assert any("weird" in p for p in problems)
    problems = validate_routing_policy({"task_class_policies": {"simple": "random"}})
    assert any("simple" in p for p in problems)


def test_validate_rejects_bad_tiers():
    for bad in (0, 4, "2", True, 2.5):
        problems = validate_routing_policy({"capability_tiers": {"deepseek:deepseek-flash": bad}})
        assert problems, f"tier {bad!r} should be rejected"
    problems = validate_routing_policy({"capability_tiers": {"deepseek-flash": 1}})
    assert any("provider:model" in p for p in problems)
    problems = validate_routing_policy({"capability_tiers": {"deepseek:": 1}})
    assert any("provider:model" in p for p in problems)


def test_validate_rejects_bad_budgets():
    assert validate_routing_policy({"monthly_budget_usd": "-1"})
    assert validate_routing_policy({"monthly_budget_usd": "not-a-number"})
    assert validate_routing_policy({"monthly_budget_usd": "1000001"})
    assert validate_routing_policy({"monthly_budget_usd": "NaN"})
    assert validate_routing_policy({"department_budgets": {"not-a-uuid": "5"}})
    assert validate_routing_policy({"department_budgets": {str(uuid.uuid4()): "-5"}})
    assert validate_routing_policy({"monthly_budget_usd": None}) == []
    assert validate_routing_policy({"monthly_budget_usd": "0"}) == []


def test_validate_rejects_unknown_fields():
    problems = validate_routing_policy({"surprise": True})
    assert any("surprise" in p for p in problems)


def test_parse_round_trip_and_effective_helpers():
    config = parse_routing_policy(FULL_POLICY)
    assert config.default_policy is RoutingPolicy.CHEAPEST
    assert config.task_class_policies == {
        TaskClass.COMPLEX: RoutingPolicy.BALANCED,
        TaskClass.VISION: RoutingPolicy.FASTEST,
    }
    assert config.monthly_budget_usd == D("25.00")
    assert effective_policy(config, TaskClass.COMPLEX) is RoutingPolicy.BALANCED
    assert effective_policy(config, TaskClass.SIMPLE) is RoutingPolicy.CHEAPEST
    assert effective_policy(DEFAULT_CONFIG, TaskClass.COMPLEX) is RoutingPolicy.CHEAPEST
    assert effective_capability_tier(config, "deepseek", "deepseek-v4-pro", 2) == 3
    assert effective_capability_tier(config, "deepseek", "deepseek-flash", 1) == 1
    dept = uuid.uuid4()
    assert effective_budget_usd(config, None) == D("25.00")
    assert effective_budget_usd(config, dept) == D("25.00")
    dept_config = parse_routing_policy({
        "monthly_budget_usd": "25.00",
        "department_budgets": {str(dept): "5.00"},
    })
    assert effective_budget_usd(dept_config, dept) == D("5.00")
    assert effective_budget_usd(dept_config, uuid.uuid4()) == D("25.00")
    assert effective_budget_usd(DEFAULT_CONFIG, dept) is None
    with pytest.raises(ValueError, match="Invalid routing policy"):
        parse_routing_policy({"default_policy": "random"})


# ------------------------------------------------------------ DB: admin API


def _policy_body(admin_client):
    response = admin_client.get("/api/admin/routing-policy")
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_api_get_returns_defaults(admin_client):
    body = _policy_body(admin_client)
    assert body["default_policy"] == "cheapest"
    assert body["task_class_policies"] == {}
    assert body["capability_tiers"] == {}
    assert body["monthly_budget_usd"] is None
    assert body["department_budgets"] == {}
    assert body["task_classes"] == ["simple", "standard", "complex", "vision", "grounded_research"]
    assert body["policies"] == ["cheapest", "fastest", "balanced"]
    assert body["spent_this_month_usd"] == "0"


def test_admin_api_put_round_trips(admin_client):
    response = admin_client.put("/api/admin/routing-policy", json={
        "default_policy": "balanced",
        "task_class_policies": {"complex": "fastest", "simple": "cheapest"},
        "capability_tiers": {"deepseek:deepseek-flash": 1},
        "monthly_budget_usd": "12.50",
        "department_budgets": {},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_policy"] == "balanced"
    assert body["task_class_policies"] == {"complex": "fastest", "simple": "cheapest"}
    assert body["capability_tiers"] == {"deepseek:deepseek-flash": 1}
    assert body["monthly_budget_usd"] == "12.50"
    # A second GET sees the same policy.
    assert _policy_body(admin_client)["default_policy"] == "balanced"
    # Restore the default so later tests start clean.
    admin_client.put("/api/admin/routing-policy", json={
        "default_policy": "cheapest",
        "task_class_policies": {},
        "capability_tiers": {},
        "monthly_budget_usd": None,
        "department_budgets": {},
    })


def test_admin_api_put_rejects_invalid(admin_client):
    before = _policy_body(admin_client)
    response = admin_client.put("/api/admin/routing-policy", json={"default_policy": "random"})
    assert response.status_code == 422, response.text
    response = admin_client.put("/api/admin/routing-policy", json={"monthly_budget_usd": "-5"})
    assert response.status_code == 422, response.text
    response = admin_client.put("/api/admin/routing-policy", json={"surprise": True})
    assert response.status_code == 422, response.text
    # Nothing was saved.
    assert _policy_body(admin_client)["default_policy"] == before["default_policy"]


def test_admin_api_forbidden_for_non_admin(client):
    response = client.get("/api/admin/routing-policy")
    assert response.status_code in (401, 403), response.text
    response = client.put("/api/admin/routing-policy", json={"default_policy": "fastest"})
    assert response.status_code in (401, 403), response.text


def test_admin_api_report_empty(admin_client):
    response = admin_client.get("/api/admin/routing-report")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 30
    assert body["department_id"] is None
    assert body["spent_this_month_usd"] == "0"
    assert body["monthly_budget_usd"] is None
    assert body["remaining_usd"] is None
    assert body["by_task_class"] == []


def test_admin_api_lists_departments(admin_client):
    group_id = uuid.uuid4()
    run_db(lambda c: c.execute(text(
        "INSERT INTO access_groups (id, kind, name, source)"
        " VALUES (:id, 'department', 'Routing Policy Test Dept', 'local')"
    ), {"id": group_id}))
    body = _policy_body(admin_client)
    assert {"id": str(group_id), "name": "Routing Policy Test Dept"} in body["departments"]


# ------------------------------------------------------------ DB: wiring


async def _save_policy(session, data):
    await save_routing_policy(session, parse_routing_policy(data))
    await session.commit()


def _reset_policy():
    async def _go(session):
        await save_routing_policy(session, DEFAULT_CONFIG)
        await session.commit()

    _run(_go)


def test_stored_policy_override_applies(admin_client):
    _seed(admin_client)
    _run(lambda s: _save_policy(s, {"task_class_policies": {"complex": "balanced"}}))
    runtime = _runtime(pricing=PRICING)
    try:
        async def _go(session):
            complex_routed = await runtime.get_routed_client(
                session, "analyse this invoice in detail, verify every line and reconcile it step by step"
            )
            simple_routed = await runtime.get_routed_client(session, "hi")
            return complex_routed, simple_routed

        complex_routed, simple_routed = _run(_go)
        assert complex_routed.decision.policy is RoutingPolicy.BALANCED
        assert (complex_routed.decision.provider, complex_routed.decision.model) == (
            "deepseek", "deepseek-v4-pro",
        )
        assert simple_routed.decision.policy is RoutingPolicy.CHEAPEST
        assert (simple_routed.decision.provider, simple_routed.decision.model) == (
            "deepseek", "deepseek-flash",
        )
    finally:
        _reset_policy()


def test_stored_tier_override_applies(admin_client):
    _seed(admin_client)
    _run(lambda s: _save_policy(s, {
        "task_class_policies": {"complex": "balanced"},
        "capability_tiers": {"deepseek:deepseek-v4-pro": 1},
    }))
    runtime = _runtime(pricing=PRICING)
    try:
        async def _go(session):
            return await runtime.get_routed_client(
                session, "analyse this invoice in detail, verify every line and reconcile it step by step"
            )

        routed = _run(_go)
        # With the flagship demoted to tier 1, no model meets the balanced
        # tier requirement, so the router falls back to the cheapest capable.
        assert routed.decision.policy is RoutingPolicy.BALANCED
        assert (routed.decision.provider, routed.decision.model) == ("deepseek", "deepseek-flash")
    finally:
        _reset_policy()


def test_stored_budget_downgrades_simple_and_refuses_complex(admin_client):
    _seed(admin_client)
    _run(lambda s: _save_policy(s, {"monthly_budget_usd": "0.00000001"}))
    runtime = _runtime(pricing=PRICING)
    try:
        async def _go(session):
            simple = await runtime.get_routed_client(session, "hi")
            try:
                await runtime.get_routed_client(
                    session, "analyse this invoice in detail, verify every line and reconcile it step by step"
                )
                complex_error = None
            except RoutingBudgetExceededError as exc:
                complex_error = exc
            return simple, complex_error

        simple, complex_error = _run(_go)
        assert simple.decision.budget_action == "downgrade"
        assert (simple.decision.provider, simple.decision.model) == ("deepseek", "deepseek-flash")
        assert complex_error is not None
        assert complex_error.code == "routing_budget_exceeded"
    finally:
        _reset_policy()


def test_stored_budget_allows_under_budget(admin_client):
    _seed(admin_client)
    _run(lambda s: _save_policy(s, {"monthly_budget_usd": "100"}))
    runtime = _runtime(pricing=PRICING)
    try:
        async def _go(session):
            return await runtime.get_routed_client(session, "hi")

        routed = _run(_go)
        assert routed.decision.budget_action == "allow"
    finally:
        _reset_policy()


def test_department_budget_overrides_org_budget(admin_client):
    _seed(admin_client)
    dept = uuid.uuid4()
    _run(lambda s: _save_policy(s, {
        "monthly_budget_usd": "100",
        "department_budgets": {str(dept): "0.00000001"},
    }))
    runtime = _runtime(pricing=PRICING)
    try:
        async def _go(session):
            in_dept = await runtime.get_routed_client(session, "hi", department_id=dept)
            org_wide = await runtime.get_routed_client(session, "hi")
            return in_dept, org_wide

        in_dept, org_wide = _run(_go)
        assert in_dept.decision.budget_action == "downgrade"
        assert org_wide.decision.budget_action == "allow"
    finally:
        _reset_policy()


def test_unpriced_routing_prefers_configured_model(admin_client):
    # No pricing: the router has no cost basis, so the admin's configured
    # model is the choice -- and changing it changes what answers.
    _seed(admin_client)
    runtime = _runtime()
    routed = _run(lambda s: runtime.get_routed_client(s, "hi"))
    assert routed.decision.model == "deepseek-flash"
    configure(admin_client, model="deepseek-v4-pro")
    routed = _run(lambda s: runtime.get_routed_client(s, "hi"))
    assert routed.decision.model == "deepseek-v4-pro"
    assert "provider default" in routed.decision.reason
    # With prices, the router's job stands: cheapest wins over the default.
    priced = _runtime(pricing=PRICING)
    routed = _run(lambda s: priced.get_routed_client(s, "hi"))
    assert routed.decision.model == "deepseek-flash"


def test_department_for_user_and_routing_context(admin_client):
    group_id = uuid.uuid4()
    run_db(lambda c: c.execute(text(
        "INSERT INTO access_groups (id, kind, name, source)"
        " VALUES (:id, 'department', 'Routing Context Dept', 'local')"
    ), {"id": group_id}))
    admin_id = run_db(lambda c: c.execute(
        text("SELECT id FROM users WHERE email = :email"), {"email": ADMIN_EMAIL}
    )).scalar_one()
    run_db(lambda c: c.execute(text(
        "INSERT INTO access_group_members (group_id, user_id) VALUES (:gid, :uid)"
    ), {"gid": group_id, "uid": admin_id}))

    async def _resolve(session):
        department = await department_for_user(session, admin_id)
        await save_routing_policy(session, parse_routing_policy({
            "monthly_budget_usd": "100",
            "department_budgets": {str(group_id): "0.00000001"},
        }))
        await session.commit()
        # The helpers take the user id, not the user object: the commit
        # above expires any ORM user loaded in this session, and reading
        # its attributes afterwards raises MissingGreenlet. Passing the
        # id keeps the lookup working after the commit.
        context = await routing_context_for_user(session, admin_id)
        return department, context

    try:
        department, context = _run(_resolve)
        assert department == group_id
        assert context.department_id == group_id
        assert context.monthly_budget_usd == D("0.00000001")
        assert context.config.monthly_budget_usd == D("100")
    finally:
        _reset_policy()


def test_chat_endpoint_uses_stored_budget(admin_client):
    _seed(admin_client)
    # No budget stored: the chat answers.
    response = admin_client.post("/api/ai/chat", json={"message": "hello"})
    assert response.status_code == 200, response.text
    assert response.json()["routing"]["budget_action"] == "allow"
    # A tiny stored budget: the unpriced chat model cannot prove it fits,
    # so the guard refuses (402) and a refused record is persisted.
    _run(lambda s: _save_policy(s, {"monthly_budget_usd": "0.00000001"}))
    try:
        response = admin_client.post("/api/ai/chat", json={"message": "hello"})
        assert response.status_code == 402, response.text
        # Error bodies are {"error": {"code": ..., "message": ...}}.
        assert response.json()["error"]["code"] == "routing_budget_exceeded"
        refused = run_db(lambda c: c.execute(text(
            "SELECT COUNT(*) FROM model_routing_records WHERE status = 'refused'"
        ))).scalar_one()
        assert refused >= 1
    finally:
        _reset_policy()
