"""Step W slice 7 — recovery/continuity hookup: filing incidents as recovery
events (threshold/duplicate rules apply, no playbook ever runs), and
continuity activation candidates (read-only; nothing is activated).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.self_healing.detector import Incident
from app.self_healing.recovery_hook import recovery_event_for
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.recovery_lab import Lab, LabAdapter

pytestmark = pytest.mark.usefixtures("clean_tables")


def uid(email):
    return str(run_db(lambda c: c.scalar(
        text("SELECT id FROM users WHERE email=:email"), {"email": email})))


@pytest.fixture
def recovery_env(client, tmp_path):
    login(client, ADMIN_EMAIL)
    lab = Lab(tmp_path)
    adapter = LabAdapter(lab)
    client.app.state.recovery_registry.register("lab", adapter)
    tenant = client.post("/api/recovery/workspaces", json={"name": "Self-healing QA"}).json()["id"]
    base = f"/api/recovery/{tenant}"
    for email, role in [(USER_EMAIL, "responder"), (USER2_EMAIL, "owner")]:
        assert client.put(base + "/members", json={"user_id": uid(email), "role": role}).status_code == 200
    target = client.post(base + "/targets", json={
        "name": "QA owner service", "owner_id": uid(ADMIN_EMAIL),
        "adapter": "lab", "resource": "lab"})
    assert target.status_code == 201, target.text
    env = {"client": client, "base": base, "tenant": tenant,
           "target": target.json(), "lab": lab, "adapter": adapter}
    yield env
    lab.close()


def _seed_incident(key="api_down", kind="api_down", severity="critical",
                   component="api", summary="API is down."):
    async def seed(conn):
        await conn.execute(text(
            "INSERT INTO self_healing_incidents (id, key, kind, severity, component, summary, evidence)"
            " VALUES (gen_random_uuid(), :key, :kind, :severity, :component, :summary, '')"),
            {"key": key, "kind": kind, "severity": severity,
             "component": component, "summary": summary})
    run_db(seed)


def _file(client, tenant, key):
    return client.post("/api/self-healing/recovery/file-event",
                       json={"tenant_id": tenant, "incident_key": key})


# --- pure mapping ---------------------------------------------------------

def test_recovery_event_for_maps_kind_and_scrubs():
    event = recovery_event_for(
        Incident(key="api_down", kind="api_down", severity="critical", component="api",
                 summary="API is down.", evidence="password= hunter2"),
        target_id=uuid.uuid4(),
    )
    assert event.event_key == "self-healing:api_down"
    assert event.source == "api"
    assert event.category == "unavailable"
    assert event.error_code == "self_healing.api_down"
    assert event.condition == "failure"
    assert "hunter2" not in str(event.evidence)
    assert event.evidence["self_healing_incident"] == "api_down"


def test_recovery_event_for_unknown_kind_defaults_to_anomaly():
    event = recovery_event_for(
        Incident(key="x", kind="weird", severity="low", component="thing",
                 summary="weird thing", evidence=""),
        target_id=uuid.uuid4(),
    )
    assert event.category == "anomaly"
    assert event.source == "infrastructure"


# --- filing ---------------------------------------------------------------

def test_file_event_creates_recovery_incident(recovery_env):
    _seed_incident()
    response = _file(recovery_env["client"], recovery_env["tenant"], "api_down")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "incident"
    assert body["incident_id"]
    assert body["duplicate"] is False
    detail = recovery_env["client"].get(
        recovery_env["base"] + f"/incidents/{body['incident_id']}")
    assert detail.status_code == 200
    assert detail.json()["incident"]["title"].startswith("Self-healing:")


def test_file_event_duplicate_returns_same_event(recovery_env):
    _seed_incident()
    first = _file(recovery_env["client"], recovery_env["tenant"], "api_down").json()
    second = _file(recovery_env["client"], recovery_env["tenant"], "api_down")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["event_id"] == first["event_id"]


def test_file_event_no_target_writes_nothing(recovery_env):
    client = recovery_env["client"]
    tenant = client.post("/api/recovery/workspaces", json={"name": "No target QA"}).json()["id"]
    _seed_incident()
    response = _file(client, tenant, "api_down")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "no_target"
    assert body["event_id"] is None and body["incident_id"] is None


def test_file_event_non_member_404(recovery_env):
    client = recovery_env["client"]
    tenant = client.post("/api/recovery/workspaces", json={"name": "Other QA"}).json()["id"]
    _seed_incident()
    # USER2 is an admin (promoted, like the continuity fixture does) but a
    # stranger to this workspace: the recovery gate answers 404, not 403.
    run_db(lambda c: c.execute(
        text("UPDATE users SET role='admin' WHERE email=:email"), {"email": USER2_EMAIL}))
    login(client, USER2_EMAIL)
    response = _file(client, tenant, "api_down")
    assert response.status_code == 404


def test_file_event_observer_403(recovery_env):
    client = recovery_env["client"]
    tenant = client.post("/api/recovery/workspaces", json={"name": "Observer QA"}).json()["id"]
    base = f"/api/recovery/{tenant}"
    assert client.put(base + "/members",
                      json={"user_id": uid(USER_EMAIL), "role": "observer"}).status_code == 200
    _seed_incident()
    login(client, USER_EMAIL)
    response = _file(client, tenant, "api_down")
    assert response.status_code == 403


def test_file_event_missing_incident_404(recovery_env):
    response = _file(recovery_env["client"], recovery_env["tenant"], "no_such_key")
    assert response.status_code == 404


def test_file_event_never_proposes_or_decides(recovery_env):
    _seed_incident()
    body = _file(recovery_env["client"], recovery_env["tenant"], "api_down").json()
    count = run_db(lambda c: c.scalar(text(
        "SELECT count(*) FROM recovery_commands WHERE incident_id=:id"),
        {"id": uuid.UUID(body["incident_id"])}))
    assert count == 0  # no playbook was proposed, decided, or executed


# --- continuity options ---------------------------------------------------

def _seed_plan(tenant_id, title, status, admin_id):
    async def seed(conn):
        await conn.execute(text(
            "INSERT INTO bc_plans (id, tenant_id, family_id, version, title, status, data,"
            " digest, requested_by, approval_expires_at, review_due_at)"
            " VALUES (gen_random_uuid(), :t, :f, 1, :title, :status, CAST(:data AS jsonb), :digest, :by, :exp, :rev)"),
            {"t": tenant_id, "f": str(uuid.uuid4()), "title": title, "status": status,
             "data": "{}", "digest": "a" * 64, "by": admin_id,
             "exp": datetime.now(UTC) + timedelta(days=30),
             "rev": datetime.now(UTC) + timedelta(days=30)})
    run_db(seed)


def test_continuity_options_lists_approved_and_pending_only(client):
    login(client, ADMIN_EMAIL)
    tenant = client.post("/api/continuity/tenants", json={"name": "Hook QA North"}).json()
    admin_id = uid(ADMIN_EMAIL)
    _seed_plan(tenant["id"], "Approved plan", "approved", admin_id)
    _seed_plan(tenant["id"], "In-review plan", "pending", admin_id)
    _seed_plan(tenant["id"], "Rejected plan", "rejected", admin_id)
    empty = client.post("/api/continuity/tenants", json={"name": "Hook QA Empty"}).json()
    assert empty["id"] != tenant["id"]

    response = client.get("/api/self-healing/continuity-options")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["tenant_id"] for item in items] == [tenant["id"]]
    titles = [plan["title"] for plan in items[0]["plans"]]
    assert titles == ["Approved plan", "In-review plan"]


def test_continuity_options_empty_for_non_member(client):
    login(client, ADMIN_EMAIL)
    client.post("/api/continuity/tenants", json={"name": "Hook QA Private"})
    # USER2 is an admin (promoted) but a member of no continuity workspace.
    run_db(lambda c: c.execute(
        text("UPDATE users SET role='admin' WHERE email=:email"), {"email": USER2_EMAIL}))
    login(client, USER2_EMAIL)
    response = client.get("/api/self-healing/continuity-options")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_continuity_options_unauthenticated(client):
    assert client.get("/api/self-healing/continuity-options").status_code == 401
