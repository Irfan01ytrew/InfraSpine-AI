"""Step J (AI Workforce OS), slice 7: skill API routes (DB tests).

The slice-2 skill service had no routes; the admin UI (slice 7) added them.
All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_skill_routes.py -q``
"""

import uuid

from sqlalchemy import select

from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import User
from app.main import app
from app.platform import access as platform_access
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL


async def _tx(fn, *, commit=True):
    """Run ``await fn(db)`` in one session; commit unless it raises."""
    async with app.state.sessionmaker() as db:
        try:
            result = await fn(db)
        except Exception:
            await db.rollback()
            raise
        if commit:
            await db.commit()
        return result


async def _user_id(db, email):
    return await db.scalar(select(User.id).where(User.email == email))


async def _grant_manage(db):
    """Give the admin workforce.manage + workforce.read on the org scope."""
    admin_id = await _user_id(db, ADMIN_EMAIL)
    admin = await db.get(User, admin_id)
    actor = platform_access.principal_for(admin)
    await platform_access.ensure_role(
        db, "j121-test-workforce", {"workforce.manage", "workforce.read"},
        name="J121 test workforce")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(
        db, actor, user_id=admin_id, scope_id=org.id, role_key="j121-test-workforce")
    return admin_id


async def _make_studio_agent(db, owner_email, name="Triage agent"):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name=name,
                             instructions="Triage invoices.",
                             tool_permissions=["web_search"],
                             approval_tools=[], delegate_agent_ids=[])
    db.add(agent)
    await db.flush()
    return agent.id


def test_skill_catalog_api_round_trip(admin_client, client):
    async def run(db):
        await _grant_manage(db)
        return True

    assert client.portal.call(lambda: _tx(run))
    created = admin_client.post("/api/workforce/skills", json={
        "name": "invoice-triage", "description": "Reads invoices.", "category": "finance"})
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]
    assert created.json()["name"] == "invoice-triage"

    listed = admin_client.get("/api/workforce/skills")
    assert listed.status_code == 200
    assert any(row["id"] == skill_id for row in listed.json())


def test_agent_skill_api_round_trip(admin_client, client):
    async def run(db):
        await _grant_manage(db)
        return await _make_studio_agent(db, USER_EMAIL, name="Skill agent")

    agent_id = client.portal.call(lambda: _tx(run))
    created = admin_client.post("/api/workforce/skills", json={"name": "ocr-read"})
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]

    attached = admin_client.post(f"/api/workforce/agents/studio/{agent_id}/skills",
                                 json={"skill_id": skill_id})
    assert attached.status_code == 200, attached.text
    assert attached.json()["status"] == "unverified"

    verified = admin_client.post(
        f"/api/workforce/agents/studio/{agent_id}/skills/{skill_id}/verify", json={})
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "verified"
    assert verified.json()["verified_by_user_id"] is not None

    revoked = admin_client.post(
        f"/api/workforce/agents/studio/{agent_id}/skills/{skill_id}/revoke", json={})
    assert revoked.status_code == 200, revoked.text
    # Revocation clears the verification; the derived status is unverified again.
    assert revoked.json()["status"] == "unverified"

    removed = admin_client.delete(f"/api/workforce/agents/studio/{agent_id}/skills/{skill_id}")
    assert removed.status_code == 204, removed.text
    rows = admin_client.get(f"/api/workforce/agents/studio/{agent_id}/skills")
    assert rows.status_code == 200 and rows.json() == []


def test_skill_api_gates(client):
    """A plain user without workforce.manage cannot create skills."""
    from tests.conftest import login

    login(client, USER2_EMAIL)
    denied = client.post("/api/workforce/skills", json={"name": "nope"})
    assert denied.status_code in (401, 403), denied.text
