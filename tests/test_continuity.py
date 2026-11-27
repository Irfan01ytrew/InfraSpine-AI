"""Module 08 acceptance: real migrated PostgreSQL, authenticated APIs, synthetic companies."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.test_enterprise_people import _department


def now():
    return datetime.now(UTC).isoformat()


def ev(ref="SOP-QA-001"):
    return [{"reference": ref, "note": "Synthetic acceptance evidence reviewed by the QA organization.", "observed_at": now()}]


def post(client, path, data=None, status=201):
    response = client.post(path, json=data or {})
    assert response.status_code == status, response.text
    return response.json()


def user_ids():
    return run_db(lambda c: c.execute(text("SELECT id, email FROM users"))).all()


@pytest.fixture
def company(client):
    login(client, ADMIN_EMAIL)
    tenant = post(client, '/api/continuity/tenants', {"name": "Continuity QA North"})
    path = '/api/continuity/tenants/' + tenant['id']
    ids = {email: str(key) for key, email in user_ids()}
    # Dedicated test-only membership fixture; the separate grant test covers the governed public path.
    async def seed(conn):
        for email, role in ((USER_EMAIL, 'planner'), (USER2_EMAIL, 'approver')):
            await conn.execute(text("INSERT INTO bc_members (tenant_id,user_id,role,departments,all_departments,active) VALUES (:t,:u,:r,'[]',true,true)"), {"t": tenant['id'], "u": ids[email], "r": role})
        await conn.execute(text("UPDATE users SET role='admin' WHERE email=:email"), {"email": USER2_EMAIL})
    run_db(seed)
    group = _department(client, 'QA IT Operations')
    run_db(lambda c: c.execute(text("INSERT INTO access_group_members (group_id,user_id) VALUES (:g,:u)"), {"g": group['id'], "u": ids[USER_EMAIL]}))
    dep = post(client, path + '/nodes', {"external_key": "dept-it", "name": "IT", "kind": "department", "access_group_id": group['id']})
    dep2 = post(client, path + '/nodes', {"external_key": "dept-finance", "name": "Finance", "kind": "department"})
    def node(key, kind, name, **extra):
        return post(client, path + '/nodes', {"external_key": key, "kind": kind, "name": name, "department_id": dep['id'], "basis": "verified_fact", "evidence": ev(), **extra})
    manager = node('manager', 'employee', 'IT Manager', user_id=ids[ADMIN_EMAIL], skills=['database'])
    backup = node('backup', 'employee', 'Backup Engineer', user_id=ids[USER_EMAIL], skills=['database'])
    server = node('server', 'system', 'Critical Database Server', critical=True, required_skills=['database'], site='North site')
    process = node('payroll', 'process', 'Payroll', critical=True, required_skills=['database'], site='North site')
    site = node('site', 'site', 'North site')
    customer = node('customer', 'customer', 'QA Customer')
    project = node('project', 'project', 'ERP Upgrade')
    for source, target, kind in ((manager, server, 'owns'), (manager, project, 'owns'), (process, server, 'depends_on'), (server, site, 'depends_on'), (process, customer, 'serves'), (backup, server, 'backup_for'), (backup, process, 'backup_for'), (backup, project, 'backup_for')):
        post(client, path + '/links', {"source_id": source['id'], "target_id": target['id'], "kind": kind, "basis": "verified_fact", "evidence": ev()})
    for resource in (server, process):
        post(client, path + '/bia', {"node_id": resource['id'], "operational": 5, "financial": 3, "regulatory": 4, "customer": 4, "likelihood": 3,
                                   "rto_minutes": 60, "rpo_minutes": 10, "mtpd_minutes": 240, "minimum_service_percent": 80,
                                   "rationale": "Approved synthetic planning objectives for acceptance testing.", "evidence": ev()})
    workflow = post(client, '/api/automation/workflows', {"name": "QA continuity evidence task", "department_group_id": group['id'], "trigger_type": "manual",
                    "assignee_user_id": ids[USER_EMAIL], "task_title": "Verify database handover", "task_body": "Inspect synthetic procedures and provide verification evidence.", "due_days": 1})
    return dict(tenant=tenant, path=path, ids=ids, dep=dep, dep2=dep2, manager=manager, backup=backup, server=server, process=process, workflow=workflow, group=group)


def scenario(client, c, kind='resignation'):
    return post(client, c['path'] + '/scenarios', {"name": "IT Manager resignation" if kind == 'resignation' else "Server outage", "kind": kind,
                "disrupted_ids": [c['manager' if kind == 'resignation' else 'server']['id']], "duration_minutes": 120})


def plan_input(c, workflow=True):
    return {"title": "IT continuity handover", "kind": "handover", "department_id": c['dep']['id'], "objectives": "Preserve documented database operation with an approved backup owner.",
            "review_due_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(), "evidence": ev(),
            "steps": [{"key": "handover", "title": "Transfer operating knowledge", "instructions": "Review the approved operating procedure and attach evidence of completion.",
                       "owner_id": c['backup']['id'], "resource_id": c['server']['id'], "kind": "knowledge_transfer", "due_minutes": 60,
                       "workflow_id": c['workflow']['id'] if workflow else None}]}


def approve(client, path, item, email=USER2_EMAIL):
    login(client, email)
    return post(client, path, {"approved": True, "digest": item['digest'], "reason": "Independently reviewed the exact action and evidence."}, status=200)


def activation(client, c, plan, scene):
    login(client, USER_EMAIL)
    return post(client, c['path'] + '/runs', {"plan_id": plan['id'], "scenario_id": scene['id'], "mode": "drill", "idempotency_key": str(uuid.uuid4()),
                "disruption_at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat()}, status=202)


def test_resignation_workflow_verified_recovery_and_post_review(client, company):
    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    impacts = scene['data']['analysis']['affected']
    assert {'IT Manager', 'Critical Database Server', 'Payroll', 'ERP Upgrade', 'QA Customer'} <= {i['name'] for i in impacts}
    assert any(g['code'] == 'missing_knowledge' for g in scene['data']['analysis']['gaps'])
    assert client.get(c['path'] + f"/scenarios/{scene['id']}/handover").json()['steps']
    plan = post(client, c['path'] + '/plans', plan_input(c))
    assert client.post(c['path'] + f"/plans/{plan['id']}/decision", json={"approved": True, "digest": plan['digest'], "reason": "Attempting self approval must fail."}).status_code == 403
    assert client.post(c['path'] + '/runs', json={"plan_id": plan['id'], "mode": "drill", "idempotency_key": str(uuid.uuid4()), "disruption_at": now()}).status_code == 409
    approve(client, c['path'] + f"/plans/{plan['id']}/decision", plan)
    run = activation(client, c, plan, scene)
    assert client.get(c['path'] + f"/runs/{run['id']}").json()['tasks'] == []
    approve(client, c['path'] + f"/runs/{run['id']}/decision", run)
    report = client.get(c['path'] + f"/runs/{run['id']}").json()
    task = report['tasks'][0]
    login(client, USER_EMAIL)
    dispatched = post(client, c['path'] + f"/tasks/{task['id']}/dispatch", status=200)
    duplicate = post(client, c['path'] + f"/tasks/{task['id']}/dispatch", status=200)
    assert duplicate['workflow_run_id'] == dispatched['workflow_run_id']
    proof = {"evidence": ev(), "outcome": "Operating knowledge reviewed and restored database validated.", "recovered_at": now(),
             "restored_point_at": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(), "service_percent": 95}
    assert client.post(c['path'] + f"/tasks/{task['id']}/evidence", json=proof).status_code == 409
    login(client, USER2_EMAIL)
    post(client, '/api/automation/runs/' + dispatched['workflow_run_id'] + '/decision', {"approved": True, "rationale": "Independent workflow execution authorization."}, status=200)
    from app.main import app
    from app.services.automation_worker import process_next_workflow_run
    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker)
    login(client, USER_EMAIL)
    external = next(t for t in client.get('/api/automation/tasks').json()['tasks'] if t['run_id'] == dispatched['workflow_run_id'])
    post(client, '/api/automation/tasks/' + external['id'] + '/complete', status=200)
    post(client, c['path'] + f"/tasks/{task['id']}/evidence", proof, status=200)
    assert client.post(c['path'] + f"/tasks/{task['id']}/verify", json={"reason": "Self verification is not permitted."}).status_code == 403
    login(client, USER2_EMAIL)
    post(client, c['path'] + f"/tasks/{task['id']}/verify", {"reason": "Verified procedure and observed recovery evidence."}, status=200)
    final = post(client, c['path'] + f"/runs/{run['id']}/close", {"summary": "The controlled handover completed with independently reviewed evidence.", "corrective_actions": ["Schedule the next quarterly documentation review."], "evidence": ev()}, status=200)
    assert final['status'] == 'closed'
    report = client.get(c['path'] + f"/runs/{run['id']}").json()
    assert report['tasks'][0]['observed']['rto_met'] is True
    assert report['tasks'][0]['observed']['rpo_met'] is True
    assert report['tasks'][0]['observed']['drill'] is True
    assert client.get(c['path'] + '/audit/verify').json()['valid'] is True
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM automation_runs WHERE idempotency_key LIKE 'continuity:%'"))) == 1
    dashboard = client.get(c['path'] + '/dashboard').json()
    assert dashboard['metrics']['verified_drills'] == 1
    assert dashboard['metrics']['plan_coverage_percent'] == 50


def test_tenant_department_export_and_role_isolation(client, company):
    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c, 'server_outage')
    plan = post(client, c['path'] + '/plans', plan_input(c, workflow=False))
    login(client, ADMIN_EMAIL)
    other = post(client, '/api/continuity/tenants', {"name": "Separate company"})
    other_path = '/api/continuity/tenants/' + other['id']
    # A platform admin has no implicit access to another tenant's data.
    login(client, USER2_EMAIL)
    assert client.get(other_path + '/dashboard').status_code == 404
    run_db(lambda conn: conn.execute(text("UPDATE bc_members SET all_departments=false, departments=CAST(:deps AS jsonb), role='viewer' WHERE tenant_id=:t AND user_id=:u"),
                                   {"deps": '["' + c['dep2']['id'] + '"]', "t": c['tenant']['id'], "u": c['ids'][USER_EMAIL]}))
    login(client, USER_EMAIL)
    dashboard = client.get(c['path'] + '/dashboard').json()
    assert [n['name'] for n in dashboard['nodes']] == ['Finance']
    assert dashboard['plans'] == [] and dashboard['scenarios'] == [] and dashboard['links'] == []
    assert client.get(c['path'] + f"/scenarios/{scene['id']}/handover").status_code == 404
    assert client.post(c['path'] + f"/plans/{plan['id']}/decision", json={"approved": True, "digest": plan['digest'], "reason": "Cannot approve hidden departmental plan."}).status_code == 404
    assert 'Critical Database Server' not in client.get(c['path'] + '/export').text
    assert client.post(c['path'] + '/nodes', json={"external_key": "forbidden", "name": "Forbidden", "kind": "system", "department_id": c['dep2']['id']}).status_code == 403
    login(client, ADMIN_EMAIL)
    assert client.post(other_path + '/links', json={"source_id": c['manager']['id'], "target_id": c['server']['id'], "kind": "owns"}).status_code == 404


def test_idempotency_suspend_resume_and_sequence(client, company):
    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    payload = plan_input(c, workflow=False)
    payload['steps'].append({**payload['steps'][0], "key": "verify", "depends_on": ['handover'], "kind": "verification"})
    plan = post(client, c['path'] + '/plans', payload)
    approve(client, c['path'] + f"/plans/{plan['id']}/decision", plan)
    login(client, USER_EMAIL)
    activation_input = {"plan_id": plan['id'], "scenario_id": scene['id'], "mode": "drill", "idempotency_key": "restart-key-001", "disruption_at": now()}
    run = post(client, c['path'] + '/runs', activation_input, status=202)
    assert post(client, c['path'] + '/runs', activation_input, status=202)['id'] == run['id']
    assert client.post(c['path'] + '/runs', json={**activation_input, "disruption_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}).status_code == 409
    approve(client, c['path'] + f"/runs/{run['id']}/decision", run)
    assert client.post(c['path'] + f"/runs/{run['id']}/decision", json={"approved": True, "digest": run['digest'], "reason": "Cannot duplicate activation approval."}).status_code == 409
    tasks = client.get(c['path'] + f"/runs/{run['id']}").json()['tasks']
    second = next(t for t in tasks if t['step_key'] == 'verify')
    login(client, USER_EMAIL)
    proof = {"evidence": ev(), "outcome": "Procedure has been validated in the isolated environment.", "recovered_at": now(), "service_percent": 100}
    assert client.post(c['path'] + f"/tasks/{second['id']}/evidence", json=proof).status_code == 409
    login(client, USER2_EMAIL)
    post(client, c['path'] + f"/runs/{run['id']}/suspend", {"reason": "Suspend the controlled exercise for review."}, status=200)
    login(client, USER_EMAIL)
    assert client.post(c['path'] + f"/tasks/{tasks[0]['id']}/dispatch").status_code == 409
    login(client, USER2_EMAIL)
    post(client, c['path'] + f"/runs/{run['id']}/resume", {"reason": "Reviewed the interruption and authorized resumption."}, status=200)
    assert len(client.get(c['path'] + f"/runs/{run['id']}").json()['tasks']) == 2


def test_incident_ingestion_dedupe_validation_and_immutable_audit(client, company):
    c = company
    login(client, USER_EMAIL)
    data = {"name": "Module 07 server incident", "kind": "server_outage", "disrupted_ids": [c['server']['id']], "duration_minutes": 240,
            "source_module": "07", "source_event_id": str(uuid.uuid4()), "evidence": ev('INC-QA-001')}
    first = post(client, c['path'] + '/scenarios', data)
    assert post(client, c['path'] + '/scenarios', data)['id'] == first['id']
    assert client.post(c['path'] + '/scenarios', json={**data, "duration_minutes": 360}).status_code == 409
    bad = plan_input(c)
    bad['steps'][0]['depends_on'] = ['handover']
    assert client.post(c['path'] + '/plans', json=bad).status_code == 422
    assert client.post(c['path'] + '/nodes', json={"external_key": "invented", "kind": "system", "name": "Unsupported fact", "basis": "verified_fact"}).status_code == 422
    with pytest.raises(Exception, match='immutable'):
        run_db(lambda conn: conn.execute(text("UPDATE bc_events SET action='tampered' WHERE tenant_id=:id"), {"id": c['tenant']['id']}))
    login(client, USER2_EMAIL)
    assert client.get(c['path'] + '/audit/verify').json()['valid'] is True
