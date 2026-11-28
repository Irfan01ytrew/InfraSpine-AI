"""Step G slice 6: graph merge seed + status — real migrated PostgreSQL."""

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_continuity import company, post  # noqa: F401  (fixtures/helpers)


def _cg_tenant(client):
    login(client, ADMIN_EMAIL)
    return post(client, "/api/company-graph/tenants", {"name": "Continuity QA Graph"}, status=201)


def _seed(client, c, cg_id, status=200):
    login(client, ADMIN_EMAIL)
    response = client.post(c["path"] + "/graph-merge/seed", json={"company_graph_tenant_id": cg_id})
    assert response.status_code == status, response.text
    return response.json()


def _status(client, c, cg_id, status=200):
    login(client, ADMIN_EMAIL)
    response = client.get(c["path"] + f"/graph-merge/status?company_graph_tenant_id={cg_id}")
    assert response.status_code == status, response.text
    return response.json()


def test_graph_merge_seed_proposes_but_writes_no_records(client, company):
    c = company
    cg = _cg_tenant(client)
    result = _seed(client, c, cg["id"])
    assert result["departments"] == 1  # only IT binds an access group
    assert result["persons"] == 3  # owner + planner + approver
    assert len(result["proposal_ids"]) == 4
    assert result["links_created"] == 4
    assert result["approval_required"] is True
    changes = run_db(lambda conn: conn.execute(text(
        "SELECT count(*) FROM company_graph_changes WHERE tenant_id = :t"), {"t": cg["id"]})).scalar_one()
    assert changes == 4
    records = run_db(lambda conn: conn.execute(text(
        "SELECT count(*) FROM company_graph_records WHERE tenant_id = :t"), {"t": cg["id"]})).scalar_one()
    assert records == 0  # proposals only; nothing approved, nothing written directly


def test_graph_merge_seed_is_idempotent_and_status_reports_links(client, company):
    c = company
    cg = _cg_tenant(client)
    first = _seed(client, c, cg["id"])
    second = _seed(client, c, cg["id"])
    assert second["proposal_ids"] == first["proposal_ids"]
    assert second["links_created"] == 0
    status = _status(client, c, cg["id"])
    assert status["bc_nodes"] == 10  # 9 fixture nodes + 1 seeded employee node for the approver
    assert status["bc_nodes_linked"] == 4
    assert status["cg_records"] == 0
    assert status["links"] == 4
    assert status["stale_sources"] == []  # the seed sync just updated last_synced_at
    unlinked = {row["name"] for row in status["bc_nodes_unlinked"]}
    assert "Finance" in unlinked  # no access group, so never seeded


def test_graph_merge_seed_requires_company_graph_ownership(client, company):
    c = company
    cg = _cg_tenant(client)
    login(client, USER_EMAIL)  # continuity planner, but not a company-graph member
    response = client.post(c["path"] + "/graph-merge/seed", json={"company_graph_tenant_id": cg["id"]})
    assert response.status_code == 404, response.text
    response = client.get(c["path"] + f"/graph-merge/status?company_graph_tenant_id={cg['id']}")
    assert response.status_code == 404, response.text
