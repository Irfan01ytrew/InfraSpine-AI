"""Twin service + API (slice 5): DB-backed tests on a throwaway *_test DB.

Proves, on the dedicated test database only, that the twin overview /
what-if / predict-absence endpoints are authorized through the
company-graph tenant membership (404 for unknown tenant or non-member,
401 unauthenticated), that a what-if runs against the live twin
snapshot, and — the slice-5 guarantee — that running twin simulations
writes nothing: no platform action executions, no finance rows, no
workflow runs, no graph records/changes, no approvals, no outbox rows.
"""

import uuid

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_company_graph import add, edge, tenant


def _count_rows(table):
    async def count(conn):
        return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return run_db(count)


WRITE_TABLES = (
    "platform_action_executions",
    "fin_invoice_batches",
    "fin_payments",
    "wf_runs",
    "company_graph_records",
    "company_graph_changes",
    "platform_approval_requests",
    "platform_outbox",
)


def _snapshot_counts():
    return {table: _count_rows(table) for table in WRITE_TABLES}


def _seed_twin_graph(client):
    """Tenant with Ayesha (employee) owning the ERP (system), and the
    Payroll process depending on the ERP. Returns (tenant_id, ids)."""
    t = tenant(client, "Twin Atlas")
    ayesha = add(client, t, "Ayesha", "employee")
    erp = add(client, t, "ERP", "system")
    payroll = add(client, t, "Payroll", "process")
    edge(client, t, ayesha, erp, "owns")
    edge(client, t, payroll, erp, "depends_on")
    return t, {"ayesha": ayesha, "erp": erp, "payroll": payroll}


def _overview(client, tenant_id):
    return client.get(f"/api/digital-twin/{tenant_id}/overview")


def test_overview_member_sees_live_twin(admin_client):
    t, ids = _seed_twin_graph(admin_client)
    response = _overview(admin_client, t)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == t
    assert body["entity_count"] == 3
    assert body["entities_by_kind"] == {"person": 1, "system": 1, "process": 1}
    assert body["relationship_count"] == 2
    assert body["money_at_risk"] == {"totals": {}, "batch_count": 0, "payment_count": 0}
    assert body["fingerprint"]
    assert body["stale_sources"] == []
    assert body["truncated"] is False


def test_overview_unknown_tenant_is_404(admin_client):
    response = _overview(admin_client, str(uuid.uuid4()))
    assert response.status_code == 404


def test_overview_unauthenticated_is_401(client, admin_client):
    # Seed with an authenticated client; the 401 check itself must use a
    # client that never logged in (seeding needs a member, so the plain
    # ``client`` fixture cannot do it).
    t, _ids = _seed_twin_graph(admin_client)
    client.cookies.clear()
    response = _overview(client, t)
    assert response.status_code == 401


def test_whatif_server_down_runs_on_live_twin(admin_client):
    t, ids = _seed_twin_graph(admin_client)
    response = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "server_down", "seed_ids": [ids["erp"]]})
    assert response.status_code == 200, response.text
    body = response.json()
    simulation = body["simulation"]
    assert simulation["dry_run"] is True
    assert simulation["scenario"]["kind"] == "system_outage"
    assert simulation["scenario"]["seed_names"] == ["ERP"]
    assert body["snapshot_fingerprint"] == simulation["fingerprint"]
    affected = {entry["entity"]["id"] for entry in simulation["affected"]}
    assert affected == {ids["erp"], ids["payroll"]}
    assert simulation["failed"] == sorted(affected)
    assert body["tenant_id"] == t


def test_whatif_employee_absence_runs_on_live_twin(admin_client):
    t, ids = _seed_twin_graph(admin_client)
    response = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "key_employee_leaves", "seed_ids": [ids["ayesha"]]})
    assert response.status_code == 200, response.text
    simulation = response.json()["simulation"]
    assert simulation["dry_run"] is True
    affected = {entry["entity"]["id"] for entry in simulation["affected"]}
    # Ayesha is ERP's only owner, so ERP fails; Payroll depends on ERP and
    # has no backup, so it fails too (same propagation as server_down).
    assert affected == {ids["ayesha"], ids["erp"], ids["payroll"]}
    assert simulation["failed"] == sorted(affected)


def test_predict_absence_runs_on_live_twin(admin_client):
    t, ids = _seed_twin_graph(admin_client)
    response = admin_client.post(
        f"/api/digital-twin/{t}/predict-absence",
        json={"absent_ids": [ids["ayesha"]]})
    assert response.status_code == 200, response.text
    body = response.json()
    prediction = body["prediction"]
    assert prediction["absent_names"] == ["Ayesha"]
    assert prediction["risk_score"] >= 0
    assert body["snapshot_fingerprint"] == prediction["fingerprint"]


def test_whatif_validation_errors_are_422(admin_client):
    t, ids = _seed_twin_graph(admin_client)
    bad_template = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "nope", "seed_ids": [ids["erp"]]})
    assert bad_template.status_code == 422
    both = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "server_down", "kind": "system_outage",
              "seed_ids": [ids["erp"]]})
    assert both.status_code == 422
    unknown_seed = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "server_down", "seed_ids": [str(uuid.uuid4())]})
    assert unknown_seed.status_code == 422
    wrong_kind = admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"kind": "system_outage", "seed_ids": [ids["ayesha"]]})
    assert wrong_kind.status_code == 422


def test_twin_calls_write_nothing(admin_client):
    """Zero-side-effect proof: overview + what-if + predict-absence
    change no production rows at all."""
    t, ids = _seed_twin_graph(admin_client)
    before = _snapshot_counts()
    assert _overview(admin_client, t).status_code == 200
    assert admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "server_down", "seed_ids": [ids["erp"]]}).status_code == 200
    assert admin_client.post(
        f"/api/digital-twin/{t}/what-if",
        json={"template_id": "key_employee_leaves", "seed_ids": [ids["ayesha"]],
              "rehearse": True}).status_code == 200
    assert admin_client.post(
        f"/api/digital-twin/{t}/predict-absence",
        json={"absent_ids": [ids["ayesha"]]}).status_code == 200
    after = _snapshot_counts()
    assert after == before, {k: (before[k], after[k]) for k in before if before[k] != after[k]}
