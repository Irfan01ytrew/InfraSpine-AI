"""Step E: one workflow runtime. Legacy automation runs execute as unified workflow runs; generic action steps
with platform approvals; plan/execute/verify phases; run timeout; stop; resume rules."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_automation import _create_workflow, _manager_department
from tests.test_finance_invoice_flow import grant, pump
from tests.test_workflow_engine import detail, publish, save, setup_scope, trigger


def test_automation_run_executes_on_unified_runtime(client):
    department, employee_id = _manager_department(client)
    workflow = _create_workflow(client, department['id'], employee_id)
    login(client, USER2_EMAIL)
    run = client.post(f"/api/automation/workflows/{workflow['id']}/runs",
                      json={'idempotency_key': str(uuid.uuid4())}).json()
    login(client, 'manager@example.com', 'manager-password-123')
    approved = client.post(f"/api/automation/runs/{run['id']}/decision",
                           json={'approved': True, 'rationale': 'Reviewed for the unified runtime test.'})
    assert approved.status_code == 200, approved.text
    pump(client, 6)

    async def state(conn):
        legacy = (await conn.execute(text('SELECT status FROM automation_runs WHERE id=:id'), {'id': run['id']})).scalar()
        wf = (await conn.execute(text(
            "SELECT r.status, r.phase, r.last_verified_node, d.origin FROM wf_runs r JOIN wf_definitions d ON d.id=r.workflow_id "
            "WHERE r.origin='automation' AND r.origin_ref=:id"), {'id': run['id']})).all()
        return legacy, wf
    legacy, wf = run_db(state)
    assert legacy == 'completed'
    assert wf == [('completed', 'done', 'action', 'automation')]  # mirrored once, verified step, done
    pump(client, 2)
    assert len(run_db(state)[1]) == 1  # no duplicate mirror


def approval_spec(required='payment.approve'):
    return {'nodes': [
        {'id': 'start', 'type': 'start', 'label': 'Start'},
        {'id': 'approve', 'type': 'action', 'label': 'Approve vendor onboarding', 'risk': 'high',
         'config': {'handler': 'platform.approval', 'auto': True, 'values': {
             'action_type': 'vendor.onboard', 'params': {'vendor': '$input.name'}, 'amount': '2500.00',
             'currency': 'PKR', 'required_permission': required, 'ttl_hours': 2}}},
        {'id': 'end', 'type': 'end', 'label': 'End'},
    ], 'edges': [{'source': 'start', 'target': 'approve'}, {'source': 'approve', 'target': 'end'}],
        'run_timeout_seconds': 3600}


def test_generic_action_with_exact_platform_approval_and_phases(client):
    scope, people = setup_scope(client)
    wid = publish(client, scope, save(client, scope, approval_spec()))
    run = trigger(client, scope, wid)
    assert run['phase'] == 'plan' and run['plan'] and run['deadline_at']
    assert [p['node_id'] for p in run['plan']] == ['start', 'approve', 'end']
    pump(client, 3)
    login(client, USER2_EMAIL)
    assert client.get('/api/approvals').json()['approvals'] == []  # needs payment.approve on the department
    grant(client, scope, people, USER2_EMAIL, 'finance.approver')
    login(client, USER2_EMAIL)
    inbox = client.get('/api/approvals').json()['approvals']
    assert len(inbox) == 1
    item = inbox[0]
    assert item['action']['params']['vendor'] == 'Synthetic employee' and item['amount'] == '2500.00'
    login(client, USER_EMAIL)  # the requester cannot approve their own request
    assert client.post(f"/api/approvals/{item['id']}/decide",
                       json={'decision': 'approve', 'presented_hash': item['action_hash']}).status_code in {403, 404}
    login(client, USER2_EMAIL)
    assert client.post(f"/api/approvals/{item['id']}/decide",
                       json={'decision': 'approve', 'presented_hash': 'f' * 64}).status_code == 409
    response = client.post(f"/api/approvals/{item['id']}/decide",
                           json={'decision': 'approve', 'presented_hash': item['action_hash']})
    assert response.status_code == 200, response.text
    pump(client, 3)
    login(client, USER_EMAIL)
    data = detail(client, scope, run['id'])
    assert data['run']['status'] == 'completed' and data['run']['phase'] == 'done'
    approve = next(s for s in data['steps'] if s['node_id'] == 'approve')
    assert approve['verified_at'] and approve['verification']['status'] == 'approved'
    kinds = [e['kind'] for e in data['events']]
    assert 'run.planned' in kinds and 'action.verifying' in kinds and 'step.verified' in kinds


def test_rejected_approval_fails_and_cannot_be_resumed(client):
    scope, people = setup_scope(client)
    wid = publish(client, scope, save(client, scope, approval_spec()))
    run = trigger(client, scope, wid)
    grant(client, scope, people, USER2_EMAIL, 'finance.approver')
    pump(client, 3)
    login(client, USER2_EMAIL)
    item = client.get('/api/approvals').json()['approvals'][0]
    client.post(f"/api/approvals/{item['id']}/decide", json={'decision': 'reject', 'presented_hash': item['action_hash']})
    pump(client, 3)
    login(client, USER_EMAIL)
    data = detail(client, scope, run['id'])
    assert data['run']['status'] == 'failed' and data['run']['failure_code'] == 'approval_rejected'
    assert data['run']['phase'] == 'recover'
    response = client.post(f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/control/resume")
    assert response.status_code == 409, response.text


def test_run_timeout_and_stop(client):
    scope, _ = setup_scope(client)
    wid = publish(client, scope, save(client, scope, approval_spec()))
    timed = trigger(client, scope, wid)
    stopped = trigger(client, scope, wid)
    pump(client, 2)
    run_db(lambda conn: conn.execute(text("UPDATE wf_runs SET deadline_at=now()-interval '1 second' WHERE id=:id"),
                                     {'id': timed['id']}))
    pump(client, 2)
    login(client, USER_EMAIL)
    assert detail(client, scope, timed['id'])['run']['failure_code'] == 'run_timeout'
    response = client.post(f"/api/workflow-engine/scopes/{scope}/runs/{stopped['id']}/control/stop")
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'cancelled'

    async def pending(conn):
        return (await conn.execute(text("SELECT status FROM platform_approval_requests ORDER BY created_at"))).scalars().all()
    assert run_db(pending)  # approvals were requested; a stopped run never consumes them


def test_templates_list_action_handlers(client):
    login(client, ADMIN_EMAIL)
    body = client.get('/api/workflow-engine/templates').json()
    names = {a['name'] for a in body['actions']}
    assert {'platform.approval', 'finance.verify_invoices', 'finance.request_payment_approvals',
            'finance.execute_payments'} <= names
    assert {'automation.run', 'agent_studio.run', 'governance.execute'} <= {a['name'] for a in body['adapters']}
