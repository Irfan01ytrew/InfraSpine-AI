"""Slice 7: resilience overview API (DB-backed).

The overview is the caller's authorized projection: continuity nodes
linked to the scope plus company-graph records the caller may read.
Restricted graph records never appear for a user who may not read them —
not even in the counts.
"""

from uuid import uuid4

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_resilience_runtime import _plan, _start
from tests.test_workflow_engine import pump, setup_scope


def _seed_continuity(scope, tenant_id, node_ids):
    """A department node linked to the scope, with a system hosting a process.

    Only a department-kind node may carry an access_group_id (unique per
    node); the resources belong to the department via department_id.
    """
    dept_id, sys_id, proc_id = node_ids

    async def _go(conn):
        await conn.execute(
            text(
                "INSERT INTO bc_tenants(id, name, revision, event_head) "
                "VALUES (:id, 'Resilience test', 0, :head)"
            ),
            {"id": tenant_id, "head": "0" * 64},
        )
        await conn.execute(
            text(
                "INSERT INTO bc_nodes(id, tenant_id, access_group_id, external_key, kind, name, data) "
                "VALUES (:id, :tenant, :scope, :key, 'department', 'Test department', '{}'::jsonb)"
            ),
            {"id": dept_id, "tenant": tenant_id, "scope": scope, "key": f"ext-{dept_id}"},
        )
        for node_id, kind, name in (
            (sys_id, "system", "Core server"),
            (proc_id, "process", "Invoice process"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO bc_nodes(id, tenant_id, department_id, external_key, kind, name, data) "
                    "VALUES (:id, :tenant, :dept, :key, :kind, :name, CAST(:data AS jsonb))"
                ),
                {
                    "id": node_id,
                    "tenant": tenant_id,
                    "dept": dept_id,
                    "key": f"ext-{node_id}",
                    "kind": kind,
                    "name": name,
                    "data": '{"critical": true}',
                },
            )
        await conn.execute(
            text(
                "INSERT INTO bc_links(id, tenant_id, source_id, target_id, kind, data) "
                "VALUES (:id, :tenant, :source, :target, 'hosts', '{}'::jsonb)"
            ),
            {"id": uuid4(), "tenant": tenant_id, "source": sys_id, "target": proc_id},
        )

    run_db(_go)


def _seed_graph(tenant_id, people, admin_only_id, shared_id):
    """One tenant-visible entity + one restricted entity (admin is a reader)."""

    async def _go(conn):
        await conn.execute(
            text("INSERT INTO company_graph_tenants(id, name) VALUES (:id, 'Resilience test')"),
            {"id": tenant_id},
        )
        for email, role in ((ADMIN_EMAIL, "owner"), (USER_EMAIL, "viewer")):
            await conn.execute(
                text(
                    "INSERT INTO company_graph_members(tenant_id, user_id, role, departments, all_departments) "
                    "VALUES (:tenant, :user, :role, '[]'::jsonb, false)"
                ),
                {"tenant": tenant_id, "user": people[email], "role": role},
            )
        for record_id, kind, name, visibility, readers in (
            (shared_id, "system", "Shared server", "tenant", "[]"),
            (admin_only_id, "person", "Payroll owner", "restricted", f'["{people[ADMIN_EMAIL]}"]'),
        ):
            await conn.execute(
                text(
                    "INSERT INTO company_graph_records(id, tenant_id, record_type, external_key, kind, name, "
                    "visibility, readers, attributes, evidence, confidence, inferred, retired, valid_from, observed_at, version) "
                    "VALUES (:id, :tenant, 'entity', :key, :kind, :name, :visibility, CAST(:readers AS jsonb), "
                    "'{}'::jsonb, '[]'::jsonb, 1, false, false, now(), now(), 1)"
                ),
                {
                    "id": record_id,
                    "tenant": tenant_id,
                    "key": f"ext-{record_id}",
                    "kind": kind,
                    "name": name,
                    "visibility": visibility,
                    "readers": readers,
                },
            )

    run_db(_go)


def test_overview_empty_scope(client):
    scope, _people = setup_scope(client)
    login(client, USER_EMAIL)
    response = client.get(f"/api/resilience/scopes/{scope}/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entities"] == 0 and body["ranked"] == [] and body["plans"] == []
    assert any("Finance rows" in note for note in body["notes"])
    assert body["freshness"] is None


def test_overview_continuity_map_ranked(client):
    scope, _people = setup_scope(client)
    dept_id, sys_id, proc_id = uuid4(), uuid4(), uuid4()
    _seed_continuity(scope, uuid4(), (dept_id, sys_id, proc_id))
    login(client, USER_EMAIL)
    response = client.get(f"/api/resilience/scopes/{scope}/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entities"] == 3 and body["relationships"] == 1
    assert body["freshness"] is not None
    ranked = {item["node_id"]: item for item in body["ranked"]}
    assert f"bc:{sys_id}" in ranked and f"bc:{proc_id}" in ranked
    assert ranked[f"bc:{sys_id}"]["risk_score"] > 0
    assert ranked[f"bc:{sys_id}"]["risk_level"] in ("critical", "high", "medium", "low")


def test_overview_graph_visibility_projection(client):
    scope, people = setup_scope(client)
    admin_only_id, shared_id = uuid4(), uuid4()
    _seed_graph(uuid4(), people, admin_only_id, shared_id)

    login(client, USER_EMAIL)
    user_body = client.get(f"/api/resilience/scopes/{scope}/overview").json()
    user_ids = {item["node_id"] for item in user_body["ranked"]}
    assert f"cg:{shared_id}" in user_ids
    # The restricted record is invisible to the plain member — not in the
    # ranking, not in the entity count, not anywhere in the response.
    assert f"cg:{admin_only_id}" not in user_ids
    assert user_body["entities"] == 1
    assert "Payroll owner" not in repr(user_body)

    login(client, ADMIN_EMAIL)
    admin_body = client.get(f"/api/resilience/scopes/{scope}/overview").json()
    admin_ids = {item["node_id"] for item in admin_body["ranked"]}
    assert f"cg:{admin_only_id}" in admin_ids
    assert admin_body["entities"] == 2


def test_simulate_endpoint_scores_disruption(client):
    scope, _people = setup_scope(client)
    dept_id, sys_id, proc_id = uuid4(), uuid4(), uuid4()
    _seed_continuity(scope, uuid4(), (dept_id, sys_id, proc_id))
    login(client, USER_EMAIL)
    response = client.post(
        f"/api/resilience/scopes/{scope}/simulate",
        json={"kind": "server_outage", "target_ids": [f"bc:{sys_id}"], "duration_days": 1},
    )
    assert response.status_code == 200, response.text
    analysis = response.json()["analysis"]
    affected = {item["id"] for item in analysis["affected"]}
    assert f"bc:{sys_id}" in affected and f"bc:{proc_id}" in affected

    bad = client.post(
        f"/api/resilience/scopes/{scope}/simulate",
        json={"kind": "meteor_strike", "target_ids": [f"bc:{sys_id}"], "duration_days": 1},
    )
    assert bad.status_code in (400, 422)


def test_audit_export_is_owner_only(client):
    scope, _people = setup_scope(client)
    login(client, USER_EMAIL)
    denied = client.get(f"/api/resilience/scopes/{scope}/audit/export?format=csv")
    assert denied.status_code == 403, denied.text

    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/resilience/scopes/{scope}/audit/export?format=csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0].startswith("created_at,actor_email,action")


def test_plan_progress_endpoint(client):
    scope, _people = setup_scope(client)
    run_id, _approvals = _start(client, scope, _plan())
    pump(client)  # the approval node is now waiting for its approval
    login(client, USER_EMAIL)
    response = client.get(f"/api/resilience/scopes/{scope}/plans/plan-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_steps"] == 2
    assert body["counts"]["done"] == 0
    assert body["steps"][0]["waiting_for_approval"] is True
