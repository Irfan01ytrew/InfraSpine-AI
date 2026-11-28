"""Step N slice 3 — cost API tests (TestClient, throwaway *_test DB).

Proves the platform-gated cost endpoints return the slice-2 ledger
figures, scope-isolate (404 for non-members, 403 for members without
``cost.read``), refuse a cost view on ``module_space``, and — the
zero-side-effect proof — that a GET writes nothing.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun, AgentStudioThread
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from tests.conftest import (
    ADMIN_EMAIL,
    PASSWORD,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
)

pytestmark = pytest.mark.usefixtures("clean_tables")

OUTSIDER_EMAIL = "outsider@example.com"

COUNT_TABLES = (
    "users",
    "user_sessions",
    "agent_studio_runs",
    "agent_studio_agents",
    "platform_scopes",
    "platform_memberships",
    "platform_roles",
    "platform_role_permissions",
    "audit_logs",
    "platform_outbox",
    "platform_approval_requests",
    "platform_action_executions",
)


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _principal(db, email):
    user = await db.scalar(select(User).where(User.email == email))
    return platform.Principal(user.id, user.email, "user", "web", user.auth_version)


async def _user_id(db, email):
    return await db.scalar(select(User.id).where(User.email == email))


def _agent(db, owner_id, name):
    agent = AgentStudioAgent(
        owner_id=owner_id, name=name, instructions="x",
        tool_permissions=[], approval_tools=[], delegate_agent_ids=[], max_steps=2,
    )
    db.add(agent)
    return agent


def _thread(db, agent, owner_id):
    thread = AgentStudioThread(
        agent_id=agent.id, owner_id=owner_id,
        history_ciphertext=b"x", history_nonce=b"y", history_key_id="v1",
    )
    db.add(thread)
    return thread


def _run(db, agent, thread, owner_id, status, cost, prompt=10, completion=20):
    row = AgentStudioRun(
        agent_id=agent.id, thread_id=thread.id, owner_id=owner_id, status=status,
        state_ciphertext=b"x", state_nonce=b"y", state_key_id="v1",
        prompt_tokens=prompt, completion_tokens=completion, llm_calls=1,
        estimated_cost_usd=None if cost is None else Decimal(cost),
        usage_by_model={},
    )
    db.add(row)
    return row


async def setup(sm, *, workspace=False, module_space=False):
    """org > Finance dept (member: user) + HR dept (member: user2).

    The admin holds a ``finance`` role (``cost.read``) on the org scope,
    so the scope chain covers every descendant scope (plan §7.3: owner
    sees org-wide cost). Department members hold ``cost.read`` on their
    own department only. Same 7 runs as the slice-2 ledger tests.
    """
    async with sm() as db:
        db.add(User(email=OUTSIDER_EMAIL, role="user", password_hash="x"))
        await db.flush()
        admin = await _principal(db, ADMIN_EMAIL)
        org = await platform.ensure_org_scope(db)
        finance = await platform.create_scope(db, kind="department", parent_id=org.id, name="Finance")
        hr = await platform.create_scope(db, kind="department", parent_id=org.id, name="HR")
        await platform.ensure_role(db, "cost_viewer", {"cost.read"})
        await platform.ensure_role(db, "finance", {"cost.read"})
        await platform.ensure_role(db, "reader", {"finance.read"})
        user_id = await _user_id(db, USER_EMAIL)
        user2_id = await _user_id(db, USER2_EMAIL)
        outsider_id = await _user_id(db, OUTSIDER_EMAIL)
        admin_id = await _user_id(db, ADMIN_EMAIL)
        await platform.grant_membership(db, admin, user_id=admin_id, scope_id=org.id, role_key="finance")
        await platform.grant_membership(db, admin, user_id=user_id, scope_id=finance.id, role_key="cost_viewer")
        await platform.grant_membership(db, admin, user_id=user2_id, scope_id=hr.id, role_key="cost_viewer")
        agent_a = _agent(db, user_id, "Agent A")
        agent_b = _agent(db, user2_id, "Agent B")
        agent_out = _agent(db, outsider_id, "Outsider Agent")
        await db.flush()
        thread_a = _thread(db, agent_a, user_id)
        thread_b = _thread(db, agent_b, user2_id)
        thread_out = _thread(db, agent_out, outsider_id)
        await db.flush()
        # Finance runs (owner = USER_EMAIL, agent A): 2 completed priced,
        # 1 failed priced, 1 completed unpriced, 1 running priced.
        _run(db, agent_a, thread_a, user_id, "completed", "1.5000")
        _run(db, agent_a, thread_a, user_id, "completed", "2.0000")
        _run(db, agent_a, thread_a, user_id, "failed", "0.5000")
        _run(db, agent_a, thread_a, user_id, "completed", None)
        _run(db, agent_a, thread_a, user_id, "running", "0.2500")
        # HR-only run (owner = USER2_EMAIL, agent B): must never leak into Finance.
        _run(db, agent_b, thread_b, user2_id, "completed", "9.0000")
        # Outsider run (no membership anywhere): must never appear.
        _run(db, agent_out, thread_out, outsider_id, "completed", "7.0000")
        extra = {}
        if workspace:
            payables = await platform.create_scope(
                db, kind="workspace", parent_id=finance.id, name="Payables"
            )
            await platform.grant_membership(
                db, admin, user_id=user_id, scope_id=payables.id, role_key="cost_viewer"
            )
            extra["payables"] = payables.id
        if module_space:
            space = await platform.create_scope(
                db, kind="module_space", parent_id=org.id, name="Desk", module="chat"
            )
            extra["space"] = space.id
        await db.commit()
        return {
            "org": org.id, "finance": finance.id, "hr": hr.id,
            "user": user_id, "user2": user2_id, "outsider": outsider_id,
            "agent_a": agent_a.id, "agent_b": agent_b.id, "agent_out": agent_out.id, **extra,
        }


def _counts():
    async def go(sm):
        async with sm() as db:
            out = {}
            for table in COUNT_TABLES:
                out[table] = await db.scalar(text(f"SELECT count(*) FROM {table}"))
            return out

    return run(go)


def _summary(client, scope_id, **params):
    return client.get(f"/api/cost-intelligence/scopes/{scope_id}/summary", params=params)


# -------------------------------------------------------------------------------- summary


def test_summary_finance_matches_ledger(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = _summary(client, s["finance"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_id"] == str(s["finance"])
    figures = body["figures"]
    assert figures["runs"] == 5
    assert figures["completed"] == 3
    assert figures["failed"] == 1
    assert figures["total_cost"] == "4.2500"
    assert figures["cost_known_count"] == 4
    assert figures["cost_unknown_count"] == 1
    assert figures["success_rate"] == "75.00"
    assert figures["failure_rate"] == "25.00"
    assert figures["cost_per_task"] == "1.0625"
    assert figures["cost_per_successful_task"] == "1.4167"
    assert figures["human_cost_status"] == "unknown"
    assert figures["human_cost"] is None
    assert figures["roi"] is None
    # Per-agent: only Agent A has Finance runs.
    assert list(body["by_agent"]) == [str(s["agent_a"])]
    assert body["by_agent"][str(s["agent_a"])]["total_cost"] == "4.2500"
    # Reconciliation: AK unmerged → recorded_unavailable, never zero.
    recon = body["reconciliation"][str(s["agent_a"])]
    assert recon["status"] == "recorded_unavailable"
    assert recon["recorded_total"] is None
    assert recon["estimated_total"] == "4.2500"
    # Routing: T unmerged → None; recorded total: None, never zero.
    assert body["routing"] is None
    assert body["recorded_total"] is None
    # Provenance names the unavailable sources instead of hiding them.
    assert body["sources"]["studio_runs"] == "ok"
    assert body["sources"]["ak_spend_ledger"].startswith("unavailable:")
    assert body["sources"]["model_router"].startswith("unavailable:")
    assert len(body["fingerprint"]) == 64 + 1 + 64  # figures + ":" + reconciliation


def test_summary_org_rollup(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = _summary(client, s["org"])
    assert resp.status_code == 200, resp.text
    figures = resp.json()["figures"]
    assert (figures["runs"], figures["total_cost"]) == (7, "20.2500")
    # Org sees every run — including the outsider's: an owner with no
    # department has runs no department can see, but they still count in
    # the org roll-up (ledger docstring).
    by_agent = resp.json()["by_agent"]
    assert len(by_agent) == 3
    assert by_agent[str(s["agent_out"])]["total_cost"] == "7.0000"


def test_finance_member_reads_own_department(client):
    s = run(lambda sm: setup(sm))
    login(client, USER_EMAIL)
    resp = _summary(client, s["finance"])
    assert resp.status_code == 200, resp.text
    figures = resp.json()["figures"]
    assert (figures["runs"], figures["total_cost"]) == (5, "4.2500")


def test_hr_member_gets_404_on_finance(client):
    s = run(lambda sm: setup(sm))
    login(client, USER2_EMAIL)
    resp = _summary(client, s["finance"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


def test_member_without_permission_gets_403(client):
    """A Finance member whose role lacks cost.read is 403, not 404."""
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            admin = await _principal(db, ADMIN_EMAIL)
            await platform.grant_membership(
                db, admin, user_id=s["user2"], scope_id=s["finance"], role_key="reader"
            )
            await db.commit()
        return s

    s = run(go)
    login(client, USER2_EMAIL)
    resp = _summary(client, s["finance"])
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"


def test_org_member_without_cost_read_gets_403(client):
    """Baseline org membership alone does not imply cost.read."""
    s = run(lambda sm: setup(sm))
    login(client, USER_EMAIL)
    resp = _summary(client, s["org"])
    assert resp.status_code == 403, resp.text


def test_unknown_scope_404(client):
    run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = _summary(client, uuid.uuid4())
    assert resp.status_code == 404, resp.text


def test_module_space_has_no_cost_view(client):
    s = run(lambda sm: setup(sm, module_space=True))
    login(client, ADMIN_EMAIL)
    resp = _summary(client, s["space"])
    assert resp.status_code == 404, resp.text


def test_workspace_summary_uses_parent_department(client):
    s = run(lambda sm: setup(sm, workspace=True))
    login(client, ADMIN_EMAIL)
    resp = _summary(client, s["payables"])
    assert resp.status_code == 200, resp.text
    figures = resp.json()["figures"]
    assert (figures["runs"], figures["total_cost"]) == (5, "4.2500")


def test_since_filter(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = _summary(client, s["finance"], since=tomorrow)
    assert resp.status_code == 200, resp.text
    assert resp.json()["figures"]["runs"] == 0
    assert resp.json()["figures"]["total_cost"] == "0.0000"
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    resp = _summary(client, s["finance"], since=yesterday)
    assert resp.json()["figures"]["runs"] == 5


def test_unauthenticated_401(client):
    s = run(lambda sm: setup(sm))
    resp = _summary(client, s["finance"])
    assert resp.status_code == 401, resp.text


# -------------------------------------------------------------------------------- agents


def test_agents_list(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_id"] == str(s["finance"])
    assert len(body["agents"]) == 1
    entry = body["agents"][0]
    assert entry["agent_id"] == str(s["agent_a"])
    assert (entry["figures"]["runs"], entry["figures"]["total_cost"]) == (5, "4.2500")
    assert entry["reconciliation"]["status"] == "recorded_unavailable"
    assert body["sources"]["ak_spend_ledger"].startswith("unavailable:")
    assert body["fingerprint"]


def test_agent_detail_and_unknown_agent(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents/{s['agent_a']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == str(s["agent_a"])
    assert (body["figures"]["runs"], body["figures"]["total_cost"]) == (5, "4.2500")
    assert body["reconciliation"]["status"] == "recorded_unavailable"
    # Agent B has no Finance runs: 404, not an empty figure set.
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents/{s['agent_b']}")
    assert resp.status_code == 404, resp.text
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


def test_agents_list_is_gated(client):
    s = run(lambda sm: setup(sm))
    login(client, USER2_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents")
    assert resp.status_code == 404, resp.text


# -------------------------------------------------------------------------------- departments


def test_departments_org_lists_each_department(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['org']}/departments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_id"] == str(s["org"])
    by_name = {d["name"]: d for d in body["departments"]}
    assert set(by_name) == {"Finance", "HR"}
    assert (by_name["Finance"]["figures"]["runs"], by_name["Finance"]["figures"]["total_cost"]) == (5, "4.2500")
    assert (by_name["HR"]["figures"]["runs"], by_name["HR"]["figures"]["total_cost"]) == (1, "9.0000")
    assert by_name["Finance"]["scope_id"] == str(s["finance"])
    assert by_name["HR"]["scope_id"] == str(s["hr"])


def test_departments_of_a_department_is_itself(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/departments")
    assert resp.status_code == 200, resp.text
    departments = resp.json()["departments"]
    assert [d["name"] for d in departments] == ["Finance"]
    assert departments[0]["figures"]["runs"] == 5


def test_departments_gated_for_non_member(client):
    s = run(lambda sm: setup(sm))
    login(client, USER2_EMAIL)
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/departments")
    assert resp.status_code == 404, resp.text


# -------------------------------------------------------------------------------- zero side effect


def test_get_writes_nothing(client):
    """The read-only proof: a GET leaves every row count unchanged.

    Snapshot is taken after login (login itself creates the session row),
    so any write the endpoint made — runs, memberships, scopes, audit
    rows, outbox events — would show up here.
    """
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    before = _counts()
    resp = _summary(client, s["finance"])
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/cost-intelligence/scopes/{s['finance']}/agents")
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/cost-intelligence/scopes/{s['org']}/departments")
    assert resp.status_code == 200, resp.text
    after = _counts()
    assert after == before
