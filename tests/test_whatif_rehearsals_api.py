"""Tests for the what-if rehearsal records + drill comparison API
(Step AL slices 4–5).

Runs against the disposable PostgreSQL test database via the repo
conftest (TestClient + migrated schema + seeded users). The rehearsal
lifecycle test proves the API is side-effect free beyond the rehearsal
row itself and its audit rows: no dispatched executions, no production
row changes. The drill test seeds one Step G drill run directly in the
continuity tables, compares it with a fresh simulation, and proves
the drill records are untouched.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_whatif_api import add, checked, counts, edge, seed_atlas, simulate, tenant, user_id

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def rehearsal_count():
    async def read(conn):
        return (await conn.execute(text("SELECT count(*) FROM whatif_rehearsals"))).scalar_one()
    return run_db(read)


def bc_task_count():
    async def read(conn):
        return (await conn.execute(text("SELECT count(*) FROM bc_tasks"))).scalar_one()
    return run_db(read)


def start_rehearsal(c, t, payload):
    return c.post(f"/api/whatif/{t}/rehearsals", json=payload)


def seed_drill(admin_id, step_resource_ids):
    """One continuity tenant + approved plan + drill run + verified tasks.

    Each plan step's ``resource_id`` is a simulated recovery step's
    entity id, so the comparison finds every predicted step. Each
    task is verified with evidence recovered 45 minutes after the
    disruption at full service.
    """
    bc_tenant = uuid.uuid4()
    plan_id = uuid.uuid4()
    run_id = uuid.uuid4()
    steps = [{"key": f"step-{i + 1}", "title": f"Recover {resource_id}",
              "kind": "recover", "owner_id": admin_id, "resource_id": resource_id,
              "due_minutes": 120}
             for i, resource_id in enumerate(step_resource_ids)]
    # Continuity's plan_for resolves every step owner/resource id as a
    # bc_node and reads crisis_team/contact_tree, so the plan data must
    # carry those keys and the referenced nodes must exist in this
    # tenant (department None + an all-departments member permits them).
    plan_data = {"steps": steps, "objective_snapshots": {},
                 "crisis_team": [], "contact_tree": []}
    disruption = NOW - timedelta(hours=2)
    recovered = (disruption + timedelta(minutes=45)).isoformat().replace("+00:00", "Z")

    async def write(conn):
        await conn.execute(text(
            "INSERT INTO bc_tenants (id, name, revision, event_head)"
            " VALUES (:id, :name, 0, :head)"),
            {"id": bc_tenant, "name": "Drill Atlas", "head": "0" * 64})
        await conn.execute(text(
            "INSERT INTO bc_members (tenant_id, user_id, role, departments, all_departments, active)"
            " VALUES (:t, :u, 'owner', '[]'::jsonb, true, true)"),
            {"t": bc_tenant, "u": uuid.UUID(admin_id)})
        for resource_id in step_resource_ids:
            await conn.execute(text(
                "INSERT INTO bc_nodes (id, tenant_id, external_key, kind, name, data)"
                " VALUES (:id, :t, :key, 'system', :name, '{}'::jsonb)"),
                {"id": uuid.UUID(resource_id), "t": bc_tenant, "key": resource_id,
                 "name": f"Resource {resource_id}"})
        await conn.execute(text(
            "INSERT INTO bc_nodes (id, tenant_id, user_id, external_key, kind, name, data)"
            " VALUES (:id, :t, :u, :key, 'employee', 'Drill owner', '{}'::jsonb)"),
            {"id": uuid.UUID(admin_id), "t": bc_tenant, "u": uuid.UUID(admin_id),
             "key": admin_id})
        await conn.execute(text(
            "INSERT INTO bc_plans (id, tenant_id, family_id, version, title, status, data, digest,"
            " requested_by, approval_expires_at, review_due_at)"
            " VALUES (:id, :t, :fam, 1, 'ERP recovery', 'approved', :data, :digest, :u,"
            " :exp, :rev)"),
            {"id": plan_id, "t": bc_tenant, "fam": uuid.uuid4(),
             "data": json.dumps(plan_data), "digest": "0" * 64,
             "u": uuid.UUID(admin_id), "exp": NOW + timedelta(days=30),
             "rev": NOW + timedelta(days=30)})
        await conn.execute(text(
            "INSERT INTO bc_runs (id, tenant_id, plan_id, idempotency_key, digest, status, mode,"
            " requested_by, approval_expires_at, disruption_at, activated_at)"
            " VALUES (:id, :t, :p, :key, :digest, 'closed', 'drill', :u, :exp, :disr, :act)"),
            {"id": run_id, "t": bc_tenant, "p": plan_id, "key": "drill-1",
             "digest": "0" * 64, "u": uuid.UUID(admin_id),
             "exp": NOW + timedelta(days=30), "disr": disruption, "act": disruption})
        for step in steps:
            await conn.execute(text(
                "INSERT INTO bc_tasks (id, tenant_id, run_id, step_key, status, evidence)"
                " VALUES (:id, :t, :r, :key, 'verified', :ev)"),
                {"id": uuid.uuid4(), "t": bc_tenant, "r": run_id, "key": step["key"],
                 "ev": json.dumps({"recovered_at": recovered, "service_percent": 100})})
        await conn.commit()
    run_db(write)
    return bc_tenant, run_id


def test_rehearsal_lifecycle_via_api(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    before = counts()
    before_rehearsals = rehearsal_count()

    started = checked(start_rehearsal(c, t, {"template_id": "server_down",
                                             "seed_ids": [ids["erp"]],
                                             "title": "ERP outage practice"}), 201)
    assert started["status"] == "in_progress"
    assert started["dry_run"] is True
    assert started["side_effects"] == []
    assert started["practice_only"] is True
    assert started["title"] == "ERP outage practice"
    assert started["scenario"]["kind"] == "system_outage"
    assert [s["number"] for s in started["steps"]] == list(range(1, len(started["steps"]) + 1))
    assert all(s["status"] == "pending" and s["practice_only"] is True for s in started["steps"])
    assert rehearsal_count() == before_rehearsals + 1

    after = counts()
    for key in ("dispatched", "executions", "records", "invoices", "users"):
        assert after[key] == before[key], key
    assert after["audit_logs"] == before["audit_logs"] + 1

    rid = started["id"]
    done = checked(c.post(f"/api/whatif/{t}/rehearsals/{rid}/check-off",
                          json={"step_number": 1, "finding": "Backup person was on leave."}))
    assert done["steps"][0]["status"] == "done"
    assert done["findings"] == ["Backup person was on leave."]
    assert done["steps"][0]["checked_off_by"] == user_id(ADMIN_EMAIL)

    skipped = checked(c.post(f"/api/whatif/{t}/rehearsals/{rid}/skip",
                             json={"step_number": 2, "reason": "Process owner is travelling."}))
    assert skipped["steps"][1]["status"] == "skipped"
    assert skipped["steps"][1]["skipped_reason"] == "Process owner is travelling."

    # The atlas yields 3 steps (ERP, payroll, batch): the last one must
    # also be practised before the run can complete.
    done3 = checked(c.post(f"/api/whatif/{t}/rehearsals/{rid}/check-off",
                           json={"step_number": 3, "finding": "Batch re-keyed by hand in 20 minutes."}))
    assert done3["steps"][2]["status"] == "done"
    assert done3["findings"] == ["Backup person was on leave.",
                                 "Batch re-keyed by hand in 20 minutes."]

    completed = checked(c.post(f"/api/whatif/{t}/rehearsals/{rid}/complete",
                               json={"summary": "Two gaps found."}))
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    assert completed["summary"] == "Two gaps found."

    listed = checked(c.get(f"/api/whatif/{t}/rehearsals"))
    assert [r["id"] for r in listed["rehearsals"]] == [rid]
    fetched = checked(c.get(f"/api/whatif/{t}/rehearsals/{rid}"))
    assert fetched["status"] == "completed"


def test_rehearsal_complete_with_pending_steps_is_422(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    started = checked(start_rehearsal(c, t, {"template_id": "server_down", "seed_ids": [ids["erp"]]}), 201)
    response = c.post(f"/api/whatif/{t}/rehearsals/{started['id']}/complete", json={})
    assert response.status_code == 422, response.text


def test_rehearsal_check_off_unknown_step_is_422(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    started = checked(start_rehearsal(c, t, {"template_id": "server_down", "seed_ids": [ids["erp"]]}), 201)
    response = c.post(f"/api/whatif/{t}/rehearsals/{started['id']}/check-off",
                      json={"step_number": 99})
    assert response.status_code == 422, response.text


def test_rehearsal_non_member_gets_404(client, admin_client):
    t = tenant(admin_client)
    ids = seed_atlas(admin_client, t)
    started = checked(start_rehearsal(admin_client, t,
                                      {"template_id": "server_down", "seed_ids": [ids["erp"]]}), 201)
    login(client, USER_EMAIL)
    # POST bodies must be valid: FastAPI validates the body before the
    # endpoint runs scope_for, so an empty body would 422 instead of 404.
    cases = (
        ("POST", f"/api/whatif/{t}/rehearsals",
         {"template_id": "server_down", "seed_ids": [ids["erp"]]}),
        ("GET", f"/api/whatif/{t}/rehearsals", None),
        ("GET", f"/api/whatif/{t}/rehearsals/{started['id']}", None),
        ("POST", f"/api/whatif/{t}/rehearsals/{started['id']}/check-off",
         {"step_number": 1}),
        ("POST", f"/api/whatif/{t}/rehearsals/{started['id']}/complete", {}),
    )
    for method, url, payload in cases:
        response = client.request(method, url, json=payload)
        assert response.status_code == 404, (method, url, response.text)


def test_scenarios_library(admin_client):
    body = checked(admin_client.get("/api/whatif/scenarios"))
    by_id = {s["template_id"]: s for s in body["scenarios"]}
    assert "server_down" in by_id
    assert by_id["server_down"]["kind"] == "system_outage"
    assert by_id["server_down"]["name"]
    assert by_id["server_down"]["description"]


def test_drill_compare_via_api_is_read_only(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    simulated = checked(simulate(c, t, {"template_id": "server_down",
                                        "seed_ids": [ids["erp"]], "rehearse": True}))
    step_ids = [step["entity_id"] for wave in simulated["recovery_plan"]["waves"] for step in wave]
    assert step_ids, "simulation produced no recovery steps"
    bc_tenant, run_id = seed_drill(user_id(ADMIN_EMAIL), step_ids)

    before = counts()
    before_tasks = bc_task_count()
    comparison = checked(c.post(f"/api/whatif/{t}/drills/{run_id}/compare", json={
        "continuity_tenant_id": str(bc_tenant),
        "template_id": "server_down", "seed_ids": [ids["erp"]]}))
    assert comparison["summary"]["coverage"] == 1.0
    assert comparison["missing"] == []
    assert comparison["extra"] == []
    assert comparison["summary"]["drill_verified_rate"] == 1.0
    assert comparison["summary"]["simulated_count"] == len(step_ids)
    assert comparison["summary"]["drill_count"] == len(step_ids)
    assert comparison["run"]["mode"] == "drill"

    after = counts()
    assert bc_task_count() == before_tasks
    for key in ("dispatched", "executions", "records", "invoices", "users"):
        assert after[key] == before[key], key
    assert after["audit_logs"] == before["audit_logs"] + 1


def test_drill_compare_non_member_continuity_gets_404(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    simulated = checked(simulate(c, t, {"template_id": "server_down",
                                        "seed_ids": [ids["erp"]], "rehearse": True}))
    step_ids = [step["entity_id"] for wave in simulated["recovery_plan"]["waves"] for step in wave]
    bc_tenant, run_id = seed_drill(user_id(ADMIN_EMAIL), step_ids)
    response = c.post(f"/api/whatif/{t}/drills/{run_id}/compare", json={
        "continuity_tenant_id": str(uuid.uuid4()),
        "template_id": "server_down", "seed_ids": [ids["erp"]]})
    assert response.status_code == 404, response.text
