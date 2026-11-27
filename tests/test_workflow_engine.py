"""Real PostgreSQL/API/worker acceptance for Module 05. No live resources."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db


def setup_scope(client):
    scope = uuid4()
    async def seed(conn):
        await conn.execute(text("INSERT INTO access_groups(id,name,kind,source) VALUES (:id,'Workflow acceptance','department','local')"), {'id': scope})
        users = (await conn.execute(text('SELECT id,email FROM users'))).all()
        for id, email in users:
            await conn.execute(text('INSERT INTO access_group_members(group_id,user_id) VALUES (:g,:u)'), {'g': scope, 'u': id})
        return {email: str(id) for id, email in users}
    people = run_db(seed)
    login(client, USER_EMAIL)
    return str(scope), people


def spec(nodes=None, edges=None):
    return {'nodes': nodes or [
        {'id':'start','type':'start','label':'Start'},
        {'id':'task','type':'task','label':'Record verified input', 'config':{'values':{'name':'$input.name'}}},
        {'id':'end','type':'end','label':'End'},
    ], 'edges': edges or [{'source':'start','target':'task'},{'source':'task','target':'end'}]}


def save(client, scope, definition=None):
    response = client.post(f'/api/workflow-engine/scopes/{scope}/workflows', json={
        'name':'Acceptance workflow','spec':definition or spec()})
    assert response.status_code == 201, response.text
    return response.json()


def publish(client, scope, draft):
    wid, vid = draft['workflow']['id'], draft['version']['id']
    path = f'/api/workflow-engine/scopes/{scope}/workflows/{wid}'
    response = client.post(path+'/review', json={'version_id':vid})
    assert response.status_code == 200, response.text
    login(client, ADMIN_EMAIL)
    response = client.post(path+'/review-decision', json={'version_id':vid,'approved':True,'note':'Reviewed executable definition and scope.'})
    assert response.status_code == 200, response.text
    login(client, USER_EMAIL)
    response = client.post(path+'/publish', json={'version_id':vid})
    assert response.status_code == 200, response.text
    return wid


def trigger(client, scope, wid, **kwargs):
    response = client.post(f'/api/workflow-engine/scopes/{scope}/workflows/{wid}/runs',
        json={'idempotency_key':str(uuid4()),'inputs':{'name':'Synthetic employee'}, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()


def detail(client, scope, rid):
    response = client.get(f'/api/workflow-engine/scopes/{scope}/runs/{rid}')
    assert response.status_code == 200, response.text
    return response.json()


def pump(client, iterations=25):
    from app.main import app
    from app.workflows.engine import tick
    for _ in range(iterations):
        # Deterministic test clock without real sleeping. This mutates only the disposable test cluster.
        run_db(lambda conn: conn.execute(text("UPDATE wf_runs SET wake_at=now() WHERE status IN ('running','queued','compensating')")))
        client.portal.call(tick, app.state.sessionmaker, app.state.cipher)


def test_definition_validation_review_versioning_and_scope(client):
    scope, people = setup_scope(client)
    draft = save(client, scope)
    wid, vid = draft['workflow']['id'], draft['version']['id']
    root = f'/api/workflow-engine/scopes/{scope}/workflows/{wid}'
    assert client.post(root+'/publish', json={'version_id':vid}).status_code == 409
    assert client.post(root+'/runs', json={'idempotency_key':'unpublished'}).status_code == 409
    assert client.post(root+'/review', json={'version_id':vid}).status_code == 200
    assert client.post(root+'/review-decision', json={'version_id':vid,'approved':True,'note':'self approval'}).status_code == 403
    login(client, ADMIN_EMAIL)
    assert client.post(root+'/review-decision', json={'version_id':vid,'approved':True,'note':'Independent review'}).status_code == 200
    login(client, USER_EMAIL)
    assert client.post(root+'/publish', json={'version_id':vid}).status_code == 200
    version2 = client.post(root+'/versions', json={'name':'New version','base_version':1,'spec':spec()})
    assert version2.status_code == 201
    assert client.post(root+'/versions', json={'name':'Stale write','base_version':1,'spec':spec()}).status_code == 409
    assert client.get(root).json()['workflow']['active_version'] == 1
    with pytest.raises(Exception, match='immutable'):
        run_db(lambda conn: conn.execute(text("UPDATE wf_versions SET spec='{}'::jsonb WHERE id=:id"), {'id':vid}))
    run_db(lambda conn: conn.execute(text('DELETE FROM access_group_members WHERE group_id=:g AND user_id=:u'), {'g':scope,'u':people[USER2_EMAIL]}))
    login(client, USER2_EMAIL)
    assert client.get(root).status_code == 403
    assert client.get(f'/api/workflow-engine/scopes/{uuid4()}/workflows/{wid}').status_code == 404


def test_sequential_idempotency_encryption_and_audit(client):
    scope, _ = setup_scope(client)
    wid = publish(client, scope, save(client, scope))
    key = str(uuid4())
    run = trigger(client, scope, wid, idempotency_key=key)
    assert trigger(client, scope, wid, idempotency_key=key)['id'] == run['id']
    conflict = client.post(f'/api/workflow-engine/scopes/{scope}/workflows/{wid}/runs', json={'idempotency_key':key,'inputs':{'name':'different'}})
    assert conflict.status_code == 409
    pump(client)
    view = detail(client, scope, run['id'])
    assert view['run']['status'] == 'completed'
    assert view['audit_verified'] is True
    assert next(s for s in view['steps'] if s['node_id']=='task')['output'] == {'name':'Synthetic employee'}
    raw = run_db(lambda conn: conn.scalar(text('SELECT payload::text FROM wf_runs WHERE id=:id'), {'id':run['id']}))
    assert 'Synthetic employee' not in raw and 'ciphertext' in raw
    with pytest.raises(Exception, match='append-only'):
        run_db(lambda conn: conn.execute(text("UPDATE wf_events SET kind='tampered' WHERE run_id=:id"), {'id':run['id']}))
    dashboard = client.get(f'/api/workflow-engine/scopes/{scope}/dashboard').json()
    assert dashboard['counts']['completed'] == 1 and dashboard['completion_rate'] == 1


def test_parallel_onboarding_tasks_and_dashboard(client):
    scope, people = setup_scope(client)
    template = next(t for t in client.get('/api/workflow-engine/templates').json()['templates'] if t['key']=='onboarding')
    wid = publish(client, scope, save(client, scope, template['spec']))
    run = trigger(client, scope, wid, inputs={'employee':'Synthetic colleague','hr_owner':people[USER_EMAIL],
        'it_owner':people[USER_EMAIL],'admin_owner':people[USER_EMAIL]})
    pump(client)
    view = detail(client, scope, run['id'])
    tasks = [s for s in view['steps'] if s['status']=='human']
    assert {s['node_id'] for s in tasks} == {'hr','it','admin'}
    assert next(s for s in view['steps'] if s['node_id']=='join')['status'] == 'blocked'
    assert client.get(f'/api/workflow-engine/scopes/{scope}/dashboard').json()['waiting_count'] == 3
    for task in tasks:
        endpoint = f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/steps/{task['id']}/complete"
        assert client.post(endpoint, json={'output':{}}).status_code == 422
        assert client.post(endpoint, json={'output':{'evidence':'Verified synthetic checklist'}}).status_code == 200
        assert client.post(endpoint, json={'output':{'evidence':'Duplicate'}}).status_code == 409
    pump(client)
    assert detail(client, scope, run['id'])['run']['status'] == 'completed'


def test_conditional_branch_timer_signal_and_simulation(client):
    scope, _ = setup_scope(client)
    definition = spec([
        {'id':'start','type':'start','label':'Start'},
        {'id':'choice','type':'decision','label':'Choose','config':{'condition':{'path':'input.amount','operator':'gte','value':100}}},
        {'id':'wait','type':'event','label':'Wait for receipt','config':{'event_name':'receipt.verified'}},
        {'id':'timer','type':'timer','label':'Delay','config':{'delay_seconds':0}},
        {'id':'end','type':'end','label':'End'},
    ], [{'source':'start','target':'choice'}, {'source':'choice','target':'wait','branch':'true'},
        {'source':'choice','target':'timer','branch':'false'}, {'source':'wait','target':'end'}, {'source':'timer','target':'end'}])
    wid = publish(client, scope, save(client, scope, definition))
    run = trigger(client, scope, wid, inputs={'amount':150})
    pump(client)
    view = detail(client, scope, run['id'])
    assert next(s for s in view['steps'] if s['node_id']=='timer')['status'] == 'skipped'
    endpoint = f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/signals"
    payload = {'name':'receipt.verified','payload':{'verified':True},'idempotency_key':'receipt-0001'}
    signal = client.post(endpoint, json=payload)
    assert signal.status_code == 202
    assert client.post(endpoint, json=payload).json()['id'] == signal.json()['id']
    pump(client)
    assert detail(client, scope, run['id'])['run']['status'] == 'completed'
    simulated = trigger(client, scope, wid, inputs={'amount':150}, simulation=True)
    pump(client)
    sim_view = detail(client, scope, simulated['id'])
    assert sim_view['run']['status'] == 'completed'
    assert any(e['kind']=='step.simulated' for e in sim_view['events'])
    assert client.get(f'/api/workflow-engine/scopes/{scope}/dashboard').json()['total_runs'] == 1


def test_expired_claim_is_fenced_and_recovery_does_not_duplicate(client):
    from app.main import app
    from app.workflows.engine import claim_next, execute_claim, record_failure
    scope, _ = setup_scope(client)
    wid = publish(client, scope, save(client, scope))
    run = trigger(client, scope, wid)
    first = client.portal.call(claim_next, app.state.sessionmaker, app.state.cipher)
    assert first is not None
    run_db(lambda conn: conn.execute(text("UPDATE wf_steps SET lease_until=now()-interval '1 second' WHERE id=:id"), {'id':str(first.step_id)}))
    assert client.portal.call(execute_claim, app.state.sessionmaker, app.state.cipher, first) is False
    assert client.portal.call(record_failure, app.state.sessionmaker, first) is False
    reclaimed = client.portal.call(claim_next, app.state.sessionmaker, app.state.cipher)
    assert reclaimed is not None and reclaimed.attempt == 2 and reclaimed.token != first.token
    assert client.portal.call(execute_claim, app.state.sessionmaker, app.state.cipher, first) is False
    assert client.portal.call(execute_claim, app.state.sessionmaker, app.state.cipher, reclaimed) is True
    assert client.portal.call(execute_claim, app.state.sessionmaker, app.state.cipher, reclaimed) is False
    pump(client)
    view = detail(client, scope, run['id'])
    assert view['run']['status'] == 'completed'
    assert len([e for e in view['events'] if e['kind']=='step.completed' and e['node_id']=='start']) == 1


def test_purchase_approval_authority_and_sla(client):
    scope, people = setup_scope(client)
    template = next(t for t in client.get('/api/workflow-engine/templates').json()['templates'] if t['key']=='procurement')
    wid = publish(client, scope, save(client, scope, template['spec']))
    run = trigger(client, scope, wid, inputs={'amount':120,'buyer_id':people[USER_EMAIL]})
    pump(client)
    step = next(s for s in detail(client, scope, run['id'])['steps'] if s['status']=='approval')
    endpoint = f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/steps/{step['id']}/decision"
    decision = {'approved':True,'note':'Reviewed purchase and authority'}
    assert client.post(endpoint,json=decision).status_code == 403
    login(client, ADMIN_EMAIL)
    assert client.post(endpoint,json=decision).status_code == 403  # admin is not financial authority by itself
    run_db(lambda conn: conn.execute(text("INSERT INTO approval_limits(id,category,user_id,max_amount,currency,is_active) VALUES (:id,'purchase',:user,1000,'USD',true)"), {'id':str(uuid4()),'user':people[ADMIN_EMAIL]}))
    assert client.post(endpoint,json=decision).status_code == 200
    pump(client)
    login(client, USER_EMAIL)
    task = next(s for s in detail(client, scope, run['id'])['steps'] if s['status']=='human')
    run_db(lambda conn: conn.execute(text("UPDATE wf_steps SET deadline_at=now()-interval '1 second' WHERE id=:id"), {'id':task['id']}))
    pump(client, 3)
    view = detail(client, scope, run['id'])
    assert len([e for e in view['events'] if e['kind']=='sla.breached']) == 1
    assert client.get(f'/api/workflow-engine/scopes/{scope}/dashboard').json()['overdue_steps'] == 1
    assert client.post(f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/steps/{task['id']}/complete",json={'output':{'evidence':'Verified fulfillment'}}).status_code == 200
    pump(client)
    assert detail(client, scope, run['id'])['run']['status']=='completed'


def test_pause_cancel_and_local_compensation(client):
    scope, _ = setup_scope(client)
    wid = publish(client, scope, save(client, scope))
    run = trigger(client, scope, wid)
    endpoint = f"/api/workflow-engine/scopes/{scope}/runs/{run['id']}/control"
    assert client.post(endpoint+'/pause').status_code == 200
    pump(client, 2)
    assert detail(client, scope, run['id'])['run']['status']=='paused'
    assert client.post(endpoint+'/resume').status_code == 200
    pump(client)
    login(client, ADMIN_EMAIL)
    assert client.post(endpoint+'/compensate').status_code == 200
    pump(client)
    assert detail(client, scope, run['id'])['run']['status']=='compensated'
    login(client, USER_EMAIL)
    other = trigger(client, scope, wid)
    assert client.post(f"/api/workflow-engine/scopes/{scope}/runs/{other['id']}/control/cancel").status_code == 200
    pump(client)
    assert detail(client, scope, other['id'])['run']['status']=='cancelled'


def test_all_templates_validate_and_unsafe_graphs_rejected(client):
    scope, _ = setup_scope(client)
    from app.workflows.definition import Definition, validate_graph
    for template in client.get('/api/workflow-engine/templates').json()['templates']:
        assert validate_graph(Definition.model_validate(template['spec'])) == [], template['key']
    invalid = spec()
    invalid['edges'].append({'source':'task','target':'start'})
    response = client.post(f'/api/workflow-engine/scopes/{scope}/validate',json=invalid)
    assert response.status_code == 200 and response.json()['valid'] is False
    secret = spec()
    secret['nodes'][1]['config']['values']={'api_key':'do-not-store'}
    assert client.post(f'/api/workflow-engine/scopes/{scope}/validate',json=secret).status_code == 422
