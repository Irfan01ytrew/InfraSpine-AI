"""Step N slice 4 — control-plane inventory API tests (TestClient, throwaway *_test DB).

Proves the read-only inventory endpoint returns the six unified
sections, scope-isolates (404 for non-members, 403 for members without
``control.read``), refuses a control view on ``module_space``, never
lists credentials or cursors, records AK as unavailable (never a silent
gap), and — the zero-side-effect proof — that a GET writes nothing.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import (
    AccessGroup,
    AccessGroupMember,
    CloudConnectorConfig,
    Document,
    User,
)
from app.db.session import test_engine_options as _test_engine_options
from app.governance.models import AgentIdentity, Member, Tenant
from app.platform.models import PlatformChannelIdentity
from tests.conftest import (
    ADMIN_EMAIL,
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
    "agent_studio_agents",
    "security_agents",
    "security_tenants",
    "security_members",
    "platform_scopes",
    "platform_memberships",
    "platform_roles",
    "platform_role_permissions",
    "platform_channel_identities",
    "access_groups",
    "access_group_members",
    "documents",
    "cloud_connector_configs",
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


async def setup(sm, *, module_space=False):
    """org > Finance dept (member: user) + HR dept (member: user2).

    The admin holds a ``controller`` role (``control.read``) on the org
    scope, so the scope chain covers every descendant scope. Department
    members hold ``control.read`` on their own department only.
    """
    async with sm() as db:
        db.add(User(email=OUTSIDER_EMAIL, role="user", password_hash="x"))
        await db.flush()
        admin = await _principal(db, ADMIN_EMAIL)
        org = await platform.ensure_org_scope(db)
        finance = await platform.create_scope(db, kind="department", parent_id=org.id, name="Finance")
        hr = await platform.create_scope(db, kind="department", parent_id=org.id, name="HR")
        await platform.ensure_role(db, "control_viewer", {"control.read"})
        await platform.ensure_role(db, "controller", {"control.read"})
        await platform.ensure_role(db, "reader", {"finance.read"})
        user_id = await _user_id(db, USER_EMAIL)
        user2_id = await _user_id(db, USER2_EMAIL)
        outsider_id = await _user_id(db, OUTSIDER_EMAIL)
        admin_id = await _user_id(db, ADMIN_EMAIL)
        await platform.grant_membership(db, admin, user_id=admin_id, scope_id=org.id, role_key="controller")
        await platform.grant_membership(db, admin, user_id=user_id, scope_id=finance.id, role_key="control_viewer")
        await platform.grant_membership(db, admin, user_id=user2_id, scope_id=hr.id, role_key="control_viewer")
        # Channel identity for the Finance member.
        db.add(PlatformChannelIdentity(
            user_id=user_id, channel="whatsapp_cloud", external_id="+920000000001",
            verified_at=datetime.now(UTC), verification_method="otp",
        ))
        # Studio agents, one per department member.
        agent_a = AgentStudioAgent(
            owner_id=user_id, name="Agent A", instructions="x",
            tool_permissions=["rag.search"], approval_tools=["finance.pay"],
            delegate_agent_ids=[], max_steps=2, rag_enabled=True,
        )
        agent_b = AgentStudioAgent(
            owner_id=user2_id, name="Agent B", instructions="x",
            tool_permissions=[], approval_tools=[], delegate_agent_ids=[], max_steps=6,
        )
        db.add(agent_a)
        db.add(agent_b)
        # Governance identity owned by the HR member.
        tenant = Tenant(name="Tenant")
        db.add(tenant)
        await db.flush()
        db.add(Member(tenant_id=tenant.id, user_id=user2_id))
        # Flush the membership on its own: the governance identity's
        # composite FK (tenant_id, owner_user_id) -> security_members
        # must see the row before the identity is inserted.
        await db.flush()
        db.add(AgentIdentity(
            tenant_id=tenant.id, owner_user_id=user2_id, name="Guard",
            tools=["approve"], enabled=True, max_calls=20, calls_used=3, window_seconds=3600,
        ))
        # Access groups: Finance (member user), HR (member user2),
        # Project X (member user, kind project).
        group_fin = AccessGroup(name="Finance", kind="department", source="local")
        group_hr = AccessGroup(name="HR", kind="department", source="local")
        group_proj = AccessGroup(name="Project X", kind="project", source="local")
        db.add(group_fin)
        db.add(group_hr)
        db.add(group_proj)
        await db.flush()
        db.add(AccessGroupMember(group_id=group_fin.id, user_id=user_id))
        db.add(AccessGroupMember(group_id=group_hr.id, user_id=user2_id))
        db.add(AccessGroupMember(group_id=group_proj.id, user_id=user_id))
        # Documents: 2 owned by user, 1 owned by user2.
        db.add(Document(
            owner_id=user_id, visibility="shared", acl_status="active",
            filename="report.pdf", file_format="pdf", storage_key="k1",
            sha256="a" * 64, size_bytes=100, status="ready",
        ))
        db.add(Document(
            owner_id=user_id, visibility="restricted", acl_status="quarantined",
            acl_quarantine_reason="policy",
            filename="secret.pdf", file_format="pdf", storage_key="k2",
            sha256="b" * 64, size_bytes=200, status="ready",
        ))
        db.add(Document(
            owner_id=user2_id, visibility="private", acl_status="active",
            filename="notes.pdf", file_format="pdf", storage_key="k3",
            sha256="c" * 64, size_bytes=300, status="ready",
        ))
        # Connectors: Finance dept, HR dept, and one org-level (no group).
        db.add(CloudConnectorConfig(
            name="Drive Finance", connector_type="google_drive", visibility="restricted",
            department_group_id=group_fin.id, sync_interval_minutes=60,
            credential_ciphertext=b"x", credential_nonce=b"y", credential_key_id="v1",
        ))
        db.add(CloudConnectorConfig(
            name="Drive HR", connector_type="onedrive", visibility="shared",
            department_group_id=group_hr.id, sync_interval_minutes=60,
            credential_ciphertext=b"x", credential_nonce=b"y", credential_key_id="v1",
        ))
        db.add(CloudConnectorConfig(
            name="Drive Org", connector_type="sharepoint", visibility="shared",
            department_group_id=None, sync_interval_minutes=60,
            credential_ciphertext=b"x", credential_nonce=b"y", credential_key_id="v1",
        ))
        extra = {}
        if module_space:
            space = await platform.create_scope(
                db, kind="module_space", parent_id=org.id, name="Desk", module="chat"
            )
            extra["space"] = space.id
        await db.commit()
        return {
            "org": org.id, "finance": finance.id, "hr": hr.id,
            "user": user_id, "user2": user2_id, "outsider": outsider_id,
            "agent_a": agent_a.id, "agent_b": agent_b.id,
            "group_fin": group_fin.id, "group_hr": group_hr.id, **extra,
        }


def _counts():
    async def go(sm):
        async with sm() as db:
            out = {}
            for table in COUNT_TABLES:
                out[table] = await db.scalar(text(f"SELECT count(*) FROM {table}"))
            return out

    return run(go)


def _inventory(client, scope_id):
    return client.get(f"/api/cost-intelligence/scopes/{scope_id}/inventory")


def _section(body, key):
    for section in body["sections"]:
        if section["key"] == key:
            return section
    raise AssertionError(f"missing section {key}")


# -------------------------------------------------------------------------------- org inventory


def test_org_inventory_lists_everything(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    resp = _inventory(client, s["org"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_kind"] == "org"
    assert set(body["counts"]) == {
        "identities", "channel_identities", "policies", "agents",
        "workflow_templates", "knowledge_acls", "integrations",
    }
    # Identities: admin (org) + user (Finance) + user2 (HR) + the outsider,
    # who holds a baseline org membership mirrored from users (source
    # legacy:users) even though they hold no department membership.
    identities = _section(body, "identities")
    assert {item["email"] for item in identities["items"]} == {
        ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, OUTSIDER_EMAIL,
    }
    assert {"controller", "control_viewer"} <= {item["role_key"] for item in identities["items"]}
    channels = _section(body, "channel_identities")
    assert len(channels["items"]) == 1
    assert channels["items"][0]["external_id"] == "+920000000001"
    # Policies: both control roles, with their permissions.
    policies = _section(body, "policies")
    by_role = {item["role_key"]: item for item in policies["items"]}
    assert by_role["controller"]["permissions"] == ["control.read"]
    assert by_role["control_viewer"]["member_count"] == 2
    # Agents: both Studio agents + the governance identity.
    agents = _section(body, "agents")
    by_kind = {(item["kind"], item["name"]) for item in agents["items"]}
    assert by_kind == {("studio", "Agent A"), ("studio", "Agent B"), ("governance", "Guard")}
    guard = next(item for item in agents["items"] if item["name"] == "Guard")
    assert guard["call_budget"] == {"calls_used": 3, "max_calls": 20, "window_seconds": 3600}
    assert guard["spend_limit_usd"] is None  # call budget is not money
    agent_a = next(item for item in agents["items"] if item["name"] == "Agent A")
    assert agent_a["allowed_actions"] == ["rag.search"]
    assert agent_a["approval_tools"] == ["finance.pay"]
    # Templates come from the in-code catalog.
    templates = _section(body, "workflow_templates")
    assert {item["key"] for item in templates["items"]} >= {"onboarding", "resignation"}
    # Knowledge: 3 groups + 3 document-count rows.
    knowledge = _section(body, "knowledge_acls")
    group_names = {item["name"] for item in knowledge["items"] if item["kind"] == "access_group"}
    assert group_names == {"Finance", "HR", "Project X"}
    doc_counts = {
        (item["visibility"], item["acl_status"]): item["count"]
        for item in knowledge["items"] if item["kind"] == "document_count"
    }
    assert doc_counts == {("shared", "active"): 1, ("restricted", "quarantined"): 1, ("private", "active"): 1}
    # Integrations: all three connectors, with no credential material —
    # assert the exact secret/cursor field names are absent, not just
    # the words (the sources prose itself mentions them as never listed).
    integrations = _section(body, "integrations")
    assert {item["name"] for item in integrations["items"]} == {
        "Drive Finance", "Drive HR", "Drive Org",
    }
    for secret_field in (
        "credential_ciphertext", "credential_nonce", "credential_key_id",
        "last_cursor", "permission_reconcile_cursor",
    ):
        assert secret_field not in resp.text
    # AK is unmerged on this base: recorded as unavailable, never silent.
    assert body["sources"]["ak_controls"].startswith("unavailable:")
    assert body["fingerprint"]
    assert len(body["fingerprint"]) == 64


def test_finance_inventory_is_scoped(client):
    s = run(lambda sm: setup(sm))
    login(client, USER_EMAIL)
    resp = _inventory(client, s["finance"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_kind"] == "department"
    identities = _section(body, "identities")
    assert {item["email"] for item in identities["items"]} == {USER_EMAIL}
    agents = _section(body, "agents")
    assert {(item["kind"], item["name"]) for item in agents["items"]} == {("studio", "Agent A")}
    knowledge = _section(body, "knowledge_acls")
    group_names = {item["name"] for item in knowledge["items"] if item["kind"] == "access_group"}
    assert group_names == {"Finance", "Project X"}
    doc_counts = {
        (item["visibility"], item["acl_status"]): item["count"]
        for item in knowledge["items"] if item["kind"] == "document_count"
    }
    assert doc_counts == {("shared", "active"): 1, ("restricted", "quarantined"): 1}
    integrations = _section(body, "integrations")
    assert {item["name"] for item in integrations["items"]} == {"Drive Finance"}
    # Templates are the in-code catalog: identical in every scope.
    templates = _section(body, "workflow_templates")
    assert {item["key"] for item in templates["items"]} >= {"onboarding", "resignation"}


def test_hr_member_gets_404_on_finance(client):
    s = run(lambda sm: setup(sm))
    login(client, USER2_EMAIL)
    assert _inventory(client, s["finance"]).status_code == 404


def test_member_without_control_read_gets_403(client):
    s = run(lambda sm: setup(sm))

    async def grant_reader(sm):
        async with sm() as db:
            admin = await _principal(db, ADMIN_EMAIL)
            await platform.grant_membership(
                db, admin, user_id=s["user2"], scope_id=s["finance"], role_key="reader"
            )
            await db.commit()

    run(grant_reader)
    login(client, USER2_EMAIL)
    assert _inventory(client, s["finance"]).status_code == 403


def test_unknown_scope_404_and_module_space_404(client):
    s = run(lambda sm: setup(sm, module_space=True))
    login(client, ADMIN_EMAIL)
    assert _inventory(client, uuid.uuid4()).status_code == 404
    assert _inventory(client, s["space"]).status_code == 404


def test_unauthenticated_401(client):
    s = run(lambda sm: setup(sm))
    assert _inventory(client, s["finance"]).status_code == 401


def test_fingerprint_stable_and_get_writes_nothing(client):
    s = run(lambda sm: setup(sm))
    login(client, ADMIN_EMAIL)
    first = _inventory(client, s["org"]).json()
    before = _counts()
    second = _inventory(client, s["org"]).json()
    third = _inventory(client, s["finance"]).json()
    after = _counts()
    assert first["fingerprint"] == second["fingerprint"]
    assert first["fingerprint"] != third["fingerprint"]
    assert before == after
