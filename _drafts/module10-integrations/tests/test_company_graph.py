"""Module 03 workflows on the guarded, disposable PostgreSQL test database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db

NOW = "2026-01-01T00:00:00Z"


def checked(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def tenant(client, name="Synthetic Atlas"):
    return checked(client.post("/api/company-graph/tenants", json={"name": name}), 201)["id"]


def record_input(name, kind="system", **extra):
    return {"record_type": "entity", "external_key": name, "kind": kind, "name": name,
            "visibility": "tenant", "evidence": [{"source": "Synthetic SOP", "reference": "fixture:atlas", "quote": name, "observed_at": NOW}],
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


def test_approved_graph_resignation_outage_backup_and_consumers(admin_client):
    c = admin_client
    t = tenant(c)
    dept = add(c, t, "IT", "department")
    manager = add(c, t, "IT Manager", "employee", department_id=dept)
    backup = add(c, t, "Backup Operator", "employee", department_id=dept)
    server = add(c, t, "Critical server", department_id=dept)
    payroll = add(c, t, "Payroll", "process", department_id=dept)
    project = add(c, t, "ERP migration", "project", department_id=dept)
    edge(c, t, manager, server, "owns")
    edge(c, t, backup, server, "backup_for")
    edge(c, t, payroll, server)
    edge(c, t, project, payroll)
    graph = checked(c.get(f"/api/company-graph/{t}/map"))
    assert len(graph["entities"]) == 6 and len(graph["relationships"]) == 4
    result = checked(c.post(f"/api/company-graph/{t}/simulate", json={"failed_ids": [manager], "scenario": "resignation"}))
    affected = {x["entity"]["id"]: x for x in result["affected"]}
    assert set(affected) == {server, payroll, project}
    assert affected[server]["authorized_backups"][0]["entity"]["id"] == backup
    assert len(affected[project]["path"]) == 3
    assert result["department_ids"] == [dept]
    outage = checked(c.post(f"/api/company-graph/{t}/simulate", json={"failed_ids": [server], "scenario": "system_outage"}))
    assert {x["entity"]["id"] for x in outage["affected"]} == {payroll, project}
    answer = checked(c.post(f"/api/company-graph/{t}/query", json={"question": "What happens if IT Manager resigns?"}))
    assert len(answer["result"]["affected"]) == 3
    for consumer in ("knowledge", "agents", "workflow"):
        events = checked(c.get(f"/api/company-graph/{t}/consumers/{consumer}"))
        assert len(events["events"]) == 10
        assert checked(c.get(f"/api/company-graph/{t}/consumers/{consumer}", params={"after": events["next_cursor"]}))["events"] == []


def test_tenant_and_relationship_isolation_every_read_surface(admin_client):
    c = admin_client
    t, other = tenant(c), tenant(c, "Other company")
    member(c, t, USER_EMAIL)
    public = add(c, t, "Visible system")
    hidden = add(c, t, "Secret acquisition", visibility="restricted")
    relation = edge(c, t, public, hidden)
    login(c, USER_EMAIL)
    assert checked(c.get("/api/company-graph/tenants"))["items"][0]["id"] == t
    for path in ("map", "catalog", "quality", "export", "events", "consumers/workflow"):
        assert c.get(f"/api/company-graph/{other}/{path}").status_code == 404
        response = checked(c.get(f"/api/company-graph/{t}/{path}"))
        assert "Secret acquisition" not in str(response) and hidden not in str(response) and relation not in str(response)
    assert c.get(f"/api/company-graph/{t}/records/{hidden}").status_code == 404
    assert c.get(f"/api/company-graph/{t}/records/{relation}").status_code == 404
    assert c.get(f"/api/company-graph/{t}/neighbors/{hidden}").status_code == 404
    assert c.post(f"/api/company-graph/{t}/simulate", json={"failed_ids": [hidden]}).status_code == 404
    assert c.post(f"/api/company-graph/{t}/changes", json={"record": record_input("Denied")}).status_code == 403
    login(c, USER2_EMAIL)
    assert c.get(f"/api/company-graph/{t}/map").status_code == 404


def test_department_scope_and_filtered_history(admin_client):
    c = admin_client
    t = tenant(c)
    it, hr = add(c, t, "IT", "department"), add(c, t, "HR", "department")
    a = add(c, t, "IT server", department_id=it, visibility="department")
    b = add(c, t, "HR payroll", department_id=hr, visibility="department")
    edge(c, t, a, b)
    member(c, t, USER_EMAIL, departments=[it])
    login(c, USER_EMAIL)
    data = checked(c.get(f"/api/company-graph/{t}/map"))
    assert a in str(data) and b not in str(data)
    assert data["relationships"] == []
    assert b not in str(checked(c.get(f"/api/company-graph/{t}/events")))


def test_approval_required_replay_and_version_conflicts(admin_client):
    c, t = admin_client, tenant(admin_client)
    pending = propose(c, t, record_input("Approved later"))
    assert checked(c.get(f"/api/company-graph/{t}/map"))["entities"] == []
    approve(c, t, pending)
    assert c.post(f"/api/company-graph/{t}/changes/{pending['id']}/decision", json={"approve": True, "reason": "Replay"}).status_code == 409
    a = propose(c, t, record_input("Approved later", attributes={"revision": 2}), pending["subject_id"], 1)
    b = propose(c, t, record_input("Approved later", attributes={"revision": 3}), pending["subject_id"], 1)
    approve(c, t, a)
    assert c.post(f"/api/company-graph/{t}/changes/{b['id']}/decision", json={"approve": True, "reason": "Stale proposal"}).status_code == 409
    history = checked(c.get(f"/api/company-graph/{t}/events", params={"subject_id": pending["subject_id"]}))
    assert [e["record"]["version"] for e in history["events"]] == [1, 2]


def test_rejected_and_expired_changes_cannot_publish(admin_client):
    c, t = admin_client, tenant(admin_client)
    rejected = propose(c, t, record_input("Rejected"))
    checked(c.post(f"/api/company-graph/{t}/changes/{rejected['id']}/decision", json={"approve": False, "reason": "Evidence disagrees"}))
    expired = propose(c, t, record_input("Expired"))
    run_db(lambda conn: conn.execute(text("UPDATE company_graph_changes SET expires_at=now()-interval '1 hour' WHERE id=:id"), {"id": uuid.UUID(expired["id"])}))
    for change in (rejected, expired):
        assert c.post(f"/api/company-graph/{t}/changes/{change['id']}/decision", json={"approve": True, "reason": "Cannot publish"}).status_code == 409
    assert checked(c.get(f"/api/company-graph/{t}/map"))["entities"] == []


def test_membership_approval_last_owner_and_revocation(admin_client):
    c, t = admin_client, tenant(admin_client)
    change = checked(c.post(f"/api/company-graph/{t}/members/changes", json={"user_id": user_id(USER_EMAIL), "role": "editor"}), 201)
    login(c, USER_EMAIL)
    assert c.get(f"/api/company-graph/{t}/map").status_code == 404
    login(c, ADMIN_EMAIL)
    approve(c, t, change)
    last_owner = checked(c.post(f"/api/company-graph/{t}/members/changes", json={"user_id": user_id(ADMIN_EMAIL), "role": "viewer", "base_version": 1}), 201)
    assert c.post(f"/api/company-graph/{t}/changes/{last_owner['id']}/decision", json={"approve": True, "reason": "Remove final owner"}).status_code == 409
    member(c, t, USER_EMAIL, "editor", base_version=1, active=False)
    login(c, USER_EMAIL)
    assert c.get(f"/api/company-graph/{t}/map").status_code == 404


def test_import_discovery_deduplication_and_incremental_conflicts(admin_client):
    c, t = admin_client, tenant(admin_client)
    source = checked(c.post(f"/api/company-graph/{t}/sources", json={"name": "Synthetic HRMS", "kind": "hrms"}), 201)
    endpoint = f"/api/company-graph/{t}/sources/{source['id']}/sync"
    payload = {"idempotency_key": "batch-1", "observed_at": NOW, "reference": "fixture:hrms-1", "visibility": "tenant", "items": [
        {"key": "manager", "kind": "employee", "name": "IT Manager", "quote": "IT Manager owns payroll"},
        {"key": "payroll", "kind": "system", "name": "Payroll", "quote": "IT Manager owns payroll"},
        {"key": "ownership", "kind": "owns", "name": "Payroll owner", "quote": "IT Manager owns payroll", "source_key": "manager", "target_key": "payroll", "inferred": True, "confidence": .9},
    ]}
    result = checked(c.post(endpoint, json=payload))
    assert len(result["proposal_ids"]) == 3
    assert checked(c.post(endpoint, json=payload)) == result
    assert checked(c.get(f"/api/company-graph/{t}/map"))["entities"] == []
    for change_id in result["proposal_ids"]:
        approve(c, t, {"id": change_id})
    second = checked(c.post(endpoint, json={**payload, "idempotency_key": "batch-2"}))
    assert len(second["unchanged_ids"]) == 3 and second["proposal_ids"] == []
    payload["items"][0]["kind"] = "supplier"
    assert c.post(endpoint, json=payload).status_code == 409
    third = checked(c.post(endpoint, json={**payload, "idempotency_key": "batch-3"}))
    assert third["conflicts"][0]["reason"] == "identity_changed"


def test_quality_conflicts_gaps_staleness_and_backup_failure(admin_client):
    c, t = admin_client, tenant(admin_client)
    manager, backup = add(c, t, "Manager", "employee"), add(c, t, "Backup", "employee")
    server, gap = add(c, t, "Server"), add(c, t, "Orphan process", "process")
    edge(c, t, manager, server, "owns")
    edge(c, t, manager, server, "owns")
    edge(c, t, backup, server, "backup_for")
    quality = checked(c.get(f"/api/company-graph/{t}/quality"))
    assert gap in {r["id"] for r in quality["ownership_gaps"]}
    assert quality["conflicts"][0]["kind"] == "duplicate_relationship"
    assert quality["stale_records"]
    result = checked(c.post(f"/api/company-graph/{t}/simulate", json={"failed_ids": [manager, backup]}))
    assert result["affected"][0]["authorized_backups"] == []


def test_temporal_edges_cycle_and_pagination(admin_client):
    c, t = admin_client, tenant(admin_client)
    a, b, d = add(c, t, "Server A"), add(c, t, "Process B", "process"), add(c, t, "Old process", "process")
    edge(c, t, b, a)
    edge(c, t, a, b)
    edge(c, t, d, a, valid_to="2026-01-02T00:00:00Z")
    result = checked(c.post(f"/api/company-graph/{t}/simulate", json={"failed_ids": [a]}))
    assert [n["entity"]["id"] for n in result["affected"]] == [b]
    page = checked(c.get(f"/api/company-graph/{t}/map", params={"limit": 2}))
    assert page["truncated"] and page["next_cursor"]
    last = checked(c.get(f"/api/company-graph/{t}/map", params={"after": page["next_cursor"], "limit": 2}))
    assert len(last["entities"]) == 1 and not last["truncated"]
    assert len({r["id"] for r in page["entities"] + last["entities"]}) == 3


def test_cross_tenant_foreign_keys_and_forged_edges(admin_client):
    c, t = admin_client, tenant(admin_client)
    other = tenant(c, "Other")
    a, b = add(c, t, "A"), add(c, other, "B")
    payload = record_input("Invalid edge", "depends_on", record_type="relationship", source_id=a, target_id=b)
    assert c.post(f"/api/company-graph/{t}/changes", json={"record": payload}).status_code == 404
    async def violate(conn):
        await conn.execute(text("UPDATE company_graph_records SET department_id=:other WHERE id=:node"), {"other": uuid.UUID(b), "node": uuid.UUID(a)})
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        run_db(violate)


def test_input_validation_and_csrf(admin_client):
    c, t = admin_client, tenant(admin_client)
    for payload in (record_input("Bad", confidence=1.5), record_input("Bad", kind="DROP TABLE"), record_input("Bad", evidence=[]), record_input("Bad", visibility="department")):
        assert c.post(f"/api/company-graph/{t}/changes", json={"record": payload}).status_code == 422
    headers = dict(c.headers)
    c.headers.pop("X-Requested-With")
    assert c.post("/api/company-graph/tenants", json={"name": "CSRF"}).status_code == 403
    c.headers.update(headers)


def test_historical_private_values_stay_private_after_publication(admin_client):
    c, t = admin_client, tenant(admin_client)
    member(c, t, USER_EMAIL)
    a = add(c, t, "Private draft", visibility="restricted", attributes={"sensitive": "private-value"})
    change = propose(c, t, record_input("Private draft", name="Public record", visibility="tenant"), a, 1)
    approve(c, t, change)
    login(c, USER_EMAIL)
    history = checked(c.get(f"/api/company-graph/{t}/events"))
    assert "private-value" not in str(history)
    assert "Public record" in str(history)
