"""Tests for the what-if simulation API (Step AL slice 3).

Runs against the disposable PostgreSQL test database via the repo
conftest (TestClient + migrated schema + seeded users). The headline
test proves a simulation run is side-effect free: it counts dispatched
``platform_action_executions`` and production rows before/after the
call and requires them to be unchanged.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db

NOW = "2026-01-01T00:00:00Z"


def checked(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def tenant(client, name="What-if Atlas"):
    return checked(client.post("/api/company-graph/tenants", json={"name": name}), 201)["id"]


def record_input(name, kind="system", **extra):
    return {"record_type": "entity", "external_key": name, "kind": kind, "name": name,
            "visibility": "tenant", "evidence": [{"source": "Synthetic SOP", "reference": "fixture:whatif", "quote": name, "observed_at": NOW}],
            "observed_at": NOW, "valid_from": NOW, **extra}


def propose(client, t, data, record_id=None, base_version=0):
    return checked(client.post(f"/api/company-graph/{t}/changes", json={"record": data, "record_id": record_id, "base_version": base_version}), 201)


def approve(client, t, change):
    return checked(client.post(f"/api/company-graph/{t}/changes/{change['id']}/decision", json={"approve": True, "reason": "Reviewed synthetic evidence"}))


def add(client, t, name, kind="system", **extra):
    change = propose(client, t, record_input(name, kind, **extra))
    approve(client, t, change)
    return change["subject_id"]


def edge(client, t, source, target, kind="depends_on", **extra):
    return add(client, t, str(uuid.uuid4()), kind, record_type="relationship", source_id=source, target_id=target, **extra)


def user_id(email):
    async def read(conn):
        return str((await conn.execute(text("SELECT id FROM users WHERE email=:email"), {"email": email})).scalar_one())
    return run_db(read)


def member(client, t, email, role="viewer", **extra):
    change = checked(client.post(f"/api/company-graph/{t}/members/changes", json={"user_id": user_id(email), "role": role, **extra}), 201)
    return approve(client, t, change)


def counts():
    async def read(conn):
        queries = {
            "dispatched": "SELECT count(*) FROM platform_action_executions WHERE status='dispatched'",
            "executions": "SELECT count(*) FROM platform_action_executions",
            "records": "SELECT count(*) FROM company_graph_records",
            "invoices": "SELECT count(*) FROM fin_invoices",
            "users": "SELECT count(*) FROM users",
            "audit_logs": "SELECT count(*) FROM audit_logs",
        }
        return {label: (await conn.execute(text(sql))).scalar_one() for label, sql in queries.items()}
    return run_db(read)


def seed_atlas(c, t):
    """ERP system + manager + payroll process + one money batch."""
    erp = add(c, t, "ERP", "system")
    mgr = add(c, t, "Finance Manager", "employee")
    payroll = add(c, t, "Payroll", "process")
    batch = add(c, t, "March batch", "invoice_batch",
                attributes={"amount": 1500.0, "currency": "PKR"})
    edge(c, t, payroll, erp, "depends_on")
    edge(c, t, mgr, payroll, "owns")
    edge(c, t, batch, payroll, "depends_on")
    return {"erp": erp, "mgr": mgr, "payroll": payroll, "batch": batch}


def simulate(c, t, payload):
    return c.post(f"/api/whatif/{t}/simulate", json=payload)


def test_simulate_via_api_is_side_effect_free(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    before = counts()
    result = checked(simulate(c, t, {"template_id": "server_down", "seed_ids": [ids["erp"]]}))
    assert result["dry_run"] is True
    assert result["rehearsal"] is None
    assert {a["entity"]["id"] for a in result["affected"]} == {ids["erp"], ids["payroll"], ids["batch"]}
    assert result["money_at_risk"]["totals"] == {"PKR": 1500.0}
    assert result["snapshot"]["entity_count"] == 4
    assert result["snapshot"]["relationship_count"] == 3
    after = counts()
    # Zero side effects: no dispatch was created, and no production
    # row changed. Exactly one audit row records the simulation itself.
    assert after["dispatched"] == before["dispatched"] == 0
    assert after["executions"] == before["executions"]
    assert after["records"] == before["records"]
    assert after["invoices"] == before["invoices"]
    assert after["users"] == before["users"]
    assert after["audit_logs"] == before["audit_logs"] + 1


def test_simulate_rehearsal_is_practice_only(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    result = checked(simulate(c, t, {"template_id": "server_down", "seed_ids": [ids["erp"]], "rehearse": True}))
    rehearsal = result["rehearsal"]
    assert rehearsal["dry_run"] is True
    assert rehearsal["side_effects"] == []
    assert rehearsal["steps"], "rehearsal must list practice steps"
    assert all(step["practice_only"] is True for step in rehearsal["steps"])
    numbers = [step["number"] for step in rehearsal["steps"]]
    assert numbers == sorted(numbers) == list(range(1, len(numbers) + 1))


def test_simulate_by_kind_and_seed_validation(admin_client):
    c = admin_client
    t = tenant(c)
    ids = seed_atlas(c, t)
    result = checked(simulate(c, t, {"kind": "system_outage", "seed_ids": [ids["erp"]]}))
    assert {a["entity"]["id"] for a in result["affected"]} == {ids["erp"], ids["payroll"], ids["batch"]}
    for bad in (
        {"template_id": "no_such_template", "seed_ids": [ids["erp"]]},
        {"template_id": "server_down", "seed_ids": ["ghost"]},
        {"kind": "explode", "seed_ids": [ids["erp"]]},
        {"template_id": "server_down", "kind": "system_outage", "seed_ids": [ids["erp"]]},
        {"seed_ids": [ids["erp"]]},
        {"kind": "system_outage", "seed_ids": [ids["mgr"]]},
    ):
        response = simulate(c, t, bad)
        # InvalidInputError maps to 422 in this app (see app/core/errors.py).
        assert response.status_code == 422, (bad, response.text)


def test_simulate_non_member_gets_404(client, admin_client):
    t = tenant(admin_client)
    login(client, USER_EMAIL)
    response = simulate(client, t, {"template_id": "server_down", "seed_ids": ["erp"]})
    assert response.status_code == 404, response.text


def test_restricted_member_never_sees_hidden_money(client, admin_client):
    c = admin_client
    t = tenant(c)
    dept_a = add(c, t, "Dept A", "department")
    dept_b = add(c, t, "Dept B", "department")
    erp = add(c, t, "ERP", "system")
    payroll = add(c, t, "Payroll", "process")
    batch_a = add(c, t, "Batch A", "invoice_batch",
                  attributes={"amount": 1000.0, "currency": "PKR"})
    batch_b = add(c, t, "Batch B", "invoice_batch",
                  attributes={"amount": 2000.0, "currency": "PKR"},
                  visibility="department", department_id=dept_b)
    edge(c, t, payroll, erp, "depends_on")
    edge(c, t, batch_a, payroll, "depends_on")
    edge(c, t, batch_b, payroll, "depends_on")
    member(c, t, USER_EMAIL, role="viewer", departments=[dept_a])

    admin_result = checked(simulate(c, t, {"template_id": "server_down", "seed_ids": [erp]}))
    assert admin_result["money_at_risk"]["totals"] == {"PKR": 3000.0}
    assert batch_b in {a["entity"]["id"] for a in admin_result["affected"]}

    login(client, USER_EMAIL)
    viewer_result = checked(simulate(client, t, {"template_id": "server_down", "seed_ids": [erp]}))
    assert {a["entity"]["id"] for a in viewer_result["affected"]} == {erp, payroll, batch_a}
    assert viewer_result["money_at_risk"]["totals"] == {"PKR": 1000.0}
    assert viewer_result["snapshot"]["entity_count"] == 5
    # The hidden batch must not leak anywhere in the response, not
    # even as an id inside an affected entry or a money total.
    assert batch_b not in str(viewer_result)
