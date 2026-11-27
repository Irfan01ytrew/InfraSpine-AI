"""Step E acceptance: the invoice flow on the unified workflow runtime (PostgreSQL *_test only, mock bank).

50 invoices -> 32 verified with the exact 18 remaining by id; separation of duties; approval bound to the exact
payment (amount, payee, invoice set) with expiry and single use; revoke stops the next item and a time-limited
replacement continues; crash/resume without a second dispatch; untrusted document text; department scope.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.db.models import User
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, _PASSWORD_HASH, login, run_db
from tests.finance_fixture import ACCOUNTS, invoices, purchase_orders

CLERK, FATIMA, REHAN, APPROVER, TREASURY = ('clerk@example.com', 'fatima@example.com', 'rehan@example.com',
                                            'approver@example.com', 'treasury@example.com')
PEOPLE = (CLERK, FATIMA, REHAN, APPROVER, TREASURY)


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override
    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def seed_department(name='Finance'):
    group = uuid4()

    async def seed(conn):
        await conn.execute(text("INSERT INTO access_groups(id,name,kind,source) VALUES (:id,:n,'department','local')"),
                           {'id': group, 'n': name})
        for email in PEOPLE:
            await conn.execute(text('INSERT INTO users(id,email,role,password_hash,is_active,auth_version) '
                                    "VALUES (:id,:e,'user',:p,true,0) ON CONFLICT (email) DO NOTHING"),
                               {'id': uuid4(), 'e': email, 'p': _PASSWORD_HASH})
        users = (await conn.execute(text('SELECT id,email FROM users'))).all()
        for uid, _ in users:
            await conn.execute(text('INSERT INTO access_group_members(group_id,user_id) VALUES (:g,:u)'), {'g': group, 'u': uid})
        from app.platform.sync import backfill_all
        await conn.run_sync(backfill_all)
        return {email: str(uid) for uid, email in users}
    return str(group), run_db(seed)


def grant(client, group, people, email, role, valid_to=None):
    login(client, ADMIN_EMAIL)
    body = {'user_id': people[email], 'role': role}
    if valid_to:
        body['valid_to'] = valid_to.isoformat()
    response = client.post(f'/api/finance/scopes/{group}/roles', json=body)
    assert response.status_code == 201, response.text
    return response.json()['id']


def setup_finance(client, *, verifier_roles=None):
    group, people = seed_department()
    grant(client, group, people, ADMIN_EMAIL, 'finance.admin')
    grant(client, group, people, CLERK, 'finance.clerk')
    memberships = {'fatima': grant(client, group, people, FATIMA, 'finance.verifier')}
    grant(client, group, people, APPROVER, 'finance.approver')
    grant(client, group, people, TREASURY, 'finance.treasury')
    login(client, ADMIN_EMAIL)
    vendors = {}
    for name, account in ACCOUNTS.items():
        response = client.post(f'/api/finance/scopes/{group}/vendors', json={
            'name': name, 'bank_account': account, 'currency': 'PKR',
            'status': 'blocked' if 'Blocked' in name else 'active'})
        assert response.status_code == 201, response.text
        vendors[name] = response.json()['id']
    for number, vendor, amount, currency, status, received in purchase_orders():
        response = client.post(f'/api/finance/scopes/{group}/purchase-orders', json={
            'po_number': number, 'vendor_id': vendors[vendor], 'amount': amount, 'currency': currency, 'status': status})
        assert response.status_code == 201, response.text
        if received:
            response = client.post(f'/api/finance/scopes/{group}/receipts', json={
                'po_id': response.json()['id'], 'receipt_number': f'GRN-{number}', 'amount': received,
                'received_on': (date.today() - timedelta(days=12)).isoformat()})
            assert response.status_code == 201, response.text
    return group, people, memberships, vendors


def create_batch(client, group):
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches', json={'name': 'November batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    batch = response.json()['id']
    rows, texts, expected = invoices(date.today())
    response = client.post(f'/api/finance/batches/{batch}/invoices', json={'invoices': rows, 'texts': texts})
    assert response.status_code == 201, response.text
    ids = [item['id'] for item in response.json()['invoices']]
    assert len(ids) == 50
    return batch, ids, expected


def start(client, batch):
    login(client, CLERK)
    response = client.post(f'/api/finance/batches/{batch}/start')
    assert response.status_code == 202, response.text
    return response.json()['run_id']


def pump(client, iterations=12):
    from app.main import app
    from app.workflows.engine import tick
    for _ in range(iterations):
        async def wake(conn):
            await conn.execute(text("UPDATE wf_runs SET wake_at=now() WHERE status IN ('running','queued','compensating')"))
            await conn.execute(text("UPDATE wf_steps SET available_at=now() WHERE status IN ('ready','waiting')"))
        run_db(wake)
        client.portal.call(tick, app.state.sessionmaker, app.state.cipher)


def detail(client, batch, email=CLERK):
    login(client, email)
    response = client.get(f'/api/finance/batches/{batch}')
    assert response.status_code == 200, response.text
    return response.json()


def step(client, batch, node, email=CLERK):
    return next(s for s in detail(client, batch, email)['summary']['run']['steps'] if s['node_id'] == node)


def perform(client, group, run, step_id, email, expected=200):
    login(client, email)
    response = client.post(f'/api/workflow-engine/scopes/{group}/runs/{run}/steps/{step_id}/perform')
    assert response.status_code == expected, response.text
    return response


def approvals(client, email=APPROVER):
    login(client, email)
    response = client.get('/api/finance/approvals')
    assert response.status_code == 200, response.text
    return response.json()['approvals']


def decide(client, approval, email=APPROVER, decision='approve', presented=None, expected=200):
    login(client, email)
    response = client.post(f"/api/finance/approvals/{approval['approval_id']}/decide",
                           json={'decision': decision, 'presented_hash': presented or approval['action_hash']})
    assert response.status_code == expected, response.text
    return response


def verify_all(client, group, batch, run, email=FATIMA):
    perform(client, group, run, step(client, batch, 'verify')['id'], email)
    pump(client, 6)
    assert step(client, batch, 'verify')['status'] == 'completed'


# ---------------------------------------------------------------------------------------------------- tests

def test_invoice_batch_32_of_50_end_to_end(client, mock_bank):
    group, people, _, _ = setup_finance(client)
    batch, ids, expected = create_batch(client, group)
    run = start(client, batch)
    pump(client, 3)
    verify = step(client, batch, 'verify')
    assert verify['status'] == 'action'  # waits for a person holding invoice.verify

    # The clerk cannot verify (no invoice.verify); a verifier cannot start batches (no invoice.ingest).
    perform(client, group, run, verify['id'], CLERK, expected=403)
    login(client, FATIMA)
    assert client.post(f'/api/finance/batches/{batch}/start').status_code == 403

    verify_all(client, group, batch, run)
    data = detail(client, batch)
    summary = data['summary']
    assert summary['headline'] == '32 of 50 verified'
    assert summary['total'] == 50 and summary['verified'] == 32
    by_id = {i['id']: i for i in data['invoices']}
    want_remaining = [ids[n] for n, (_, reason) in enumerate(expected) if reason]
    assert len(want_remaining) == 18
    assert summary['remaining_ids'] == want_remaining  # the exact 18, by id, in batch order
    for n, (number, reason) in enumerate(expected):
        invoice = by_id[ids[n]]
        if reason is None:
            assert invoice['status'] in {'verified', 'approved'}, (number, invoice['reasons'])
        else:
            assert invoice['status'] == 'exception'
            assert reason in [r['code'] for r in invoice['reasons']], (number, invoice['reasons'])
    login(client, CLERK)
    queue = client.get('/api/finance/exceptions', params={'group_id': group}).json()['invoices']
    assert sorted(i['id'] for i in queue) == sorted(want_remaining)

    # Approval of the exact payments (one per vendor), payee account from the vendor master.
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 2
    assert {a['payee_account'] for a in pending} == {ACCOUNTS['Alpha Supplies'], ACCOUNTS['Beta Traders']}
    assert sum(len(a['action']['params']['invoice_ids']) for a in pending) == 32
    assert approvals(client, FATIMA) == []  # verifier does not see payment approvals
    decide(client, pending[0], presented='0' * 64, expected=409)  # reviewed something else
    decide(client, pending[0], email=FATIMA, expected=403)
    for approval in pending:
        decide(client, approval)
    pump(client, 4)
    pay = step(client, batch, 'pay')
    assert pay['status'] == 'action'
    perform(client, group, run, pay['id'], APPROVER, expected=403)  # approver cannot transfer
    perform(client, group, run, pay['id'], TREASURY)
    pump(client, 6)

    data = detail(client, batch)
    assert data['batch']['status'] == 'completed'
    assert data['summary']['run']['status'] == 'completed'
    assert {p['status'] for p in data['payments']} == {'verified'}
    assert all(p['external_ref'].startswith('MOCK-') for p in data['payments'])
    assert sum(i['status'] == 'paid' for i in data['invoices']) == 32
    assert mock_bank.submit_calls == 2
    verified_steps = {s['node_id'] for s in data['summary']['run']['steps'] if s['verified_at']}
    assert {'verify', 'approve', 'pay'} <= verified_steps

    login(client, CLERK)
    spend = client.get('/api/finance/spend', params={'months': 3}).json()
    total = sum(float(r['amount']) for r in spend['rows'])
    paid_total = sum(float(i['amount']) for i in data['invoices'] if i['status'] == 'paid')
    assert round(total, 2) == round(paid_total, 2)
    assert set(spend['departments']) <= {'Operations', 'Facilities', 'Finance'}

    # Untrusted text: the instruction inside INV-X-666 changed nothing.
    attack = next(i for i in data['invoices'] if i['invoice_number'] == 'INV-X-666')
    assert attack['status'] == 'exception' and attack['payment_id'] is None

    async def perms(conn):
        rows = await conn.execute(text(
            "SELECT DISTINCT rp.permission FROM platform_memberships m JOIN platform_role_permissions rp ON rp.role_id=m.role_id "
            "JOIN users u ON u.id=m.user_id WHERE u.email=:e AND m.revoked_at IS NULL"), {'e': CLERK})
        return {r[0] for r in rows}
    clerk_perms = run_db(perms)
    assert 'funds.transfer' not in clerk_perms and 'payment.approve' not in clerk_perms
    assert all(p['payee_account'] in ACCOUNTS.values() for p in data['payments'])


def test_separation_of_duties_verifier_cannot_approve(client):
    group, people, _, _ = setup_finance(client)
    grant(client, group, people, FATIMA, 'finance.approver')  # Fatima holds both duties
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    pump(client, 3)
    pending = approvals(client, FATIMA)
    assert len(pending) == 2
    response = decide(client, pending[0], email=FATIMA, expected=403)
    assert 'Separation of duties' in response.text
    decide(client, pending[0])  # a different person can

    # The approver cannot also transfer for this batch, even if granted treasury.
    grant(client, group, people, APPROVER, 'finance.treasury')
    decide(client, pending[1])
    pump(client, 4)
    perform(client, group, run, step(client, batch, 'pay')['id'], APPROVER)
    pump(client, 3)
    pay = step(client, batch, 'pay')
    assert pay['status'] == 'action'  # handed back: separation_of_duties
    assert detail(client, batch)['summary']['run']['blocked_reason'] == 'blocked: separation_of_duties'
    perform(client, group, run, pay['id'], TREASURY)
    pump(client, 6)
    assert detail(client, batch)['batch']['status'] == 'completed'


def test_approval_bound_to_exact_payment(client, mock_bank):
    group, people, _, vendors = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    pump(client, 3)
    for approval in approvals(client):
        decide(client, approval)
    pump(client, 4)
    # After approval, the payee bank account in the vendor master changes (e.g. a compromised master record).
    login(client, ADMIN_EMAIL)
    assert client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': 'Alpha Supplies', 'bank_account': 'PK77CHANGED0001', 'currency': 'PKR'}).status_code == 201
    perform(client, group, run, step(client, batch, 'pay')['id'], TREASURY)
    pump(client, 6)
    data = detail(client, batch)
    by_vendor = {p['vendor_name']: p for p in data['payments']}
    assert by_vendor['Alpha Supplies']['status'] == 'failed'
    assert by_vendor['Alpha Supplies']['failure_code'] == 'approval_invalid'
    assert by_vendor['Beta Traders']['status'] == 'verified'
    assert mock_bank.submit_calls == 1  # approval of X never executed Y
    alpha_ids = set(by_vendor['Alpha Supplies']['invoice_ids'])
    assert all(i['status'] == 'exception' and i['reasons'][0]['code'] == 'approval_invalid'
               for i in data['invoices'] if i['id'] in alpha_ids)


def test_approval_expiry_rerequest_and_single_use(client):
    group, people, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    pump(client, 3)
    first = approvals(client)
    run_db(lambda conn: conn.execute(text("UPDATE platform_approval_requests SET expires_at=now()-interval '1 minute' "
                                          "WHERE status='pending'")))
    decide(client, first[0], expected=409)  # expired
    pump(client, 3)
    second = approvals(client)
    assert len(second) == 2 and not ({a['approval_id'] for a in second} & {a['approval_id'] for a in first})
    for approval in second:
        decide(client, approval)
    pump(client, 4)
    perform(client, group, run, step(client, batch, 'pay')['id'], TREASURY)
    pump(client, 6)
    assert detail(client, batch)['batch']['status'] == 'completed'

    # Single use: the consumed approval cannot pay again.
    from app.main import app
    from app.platform.approvals import consume_approval, ActionSpec
    from app.core.errors import ConflictError

    async def reuse():
        async with app.state.sessionmaker() as db:
            row = await db.get(__import__('app.platform.models', fromlist=['x']).PlatformApprovalRequest,
                               UUID(second[0]['approval_id']))
            assert row.status == 'consumed'
            action = ActionSpec(row.action['type'], row.scope_id, row.action['params'], amount=row.amount,
                                currency=row.currency, subject=row.subject)
            with pytest.raises(ConflictError):
                await consume_approval(db, row.id, action)
    client.portal.call(reuse)


def test_revoke_stops_next_item_and_time_limited_replacement_continues(client):
    group, people, memberships, _ = setup_finance(client)
    batch, ids, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    perform(client, group, run, step(client, batch, 'verify')['id'], FATIMA)
    pump(client, 1)  # first slice: 25 invoices
    checked = [i['id'] for i in detail(client, batch)['invoices'] if i['status'] != 'pending']
    assert len(checked) == 25
    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/finance/scopes/{group}/roles/{memberships['fatima']}").status_code == 204
    pump(client, 3)
    data = detail(client, batch)
    remaining = [i['id'] for i in data['invoices'] if i['status'] == 'pending']
    assert remaining == ids[25:]  # exactly the 25 not yet checked, by id
    verify = next(s for s in data['summary']['run']['steps'] if s['node_id'] == 'verify')
    assert verify['status'] == 'action' and verify['acting_user_id'] is None
    assert data['summary']['run']['blocked_reason'] == 'blocked: actor_revoked'
    perform(client, group, run, verify['id'], FATIMA, expected=403)  # access is gone

    grant(client, group, people, REHAN, 'finance.verifier', valid_to=datetime.now(UTC) + timedelta(days=7))
    perform(client, group, run, verify['id'], REHAN)
    pump(client, 6)
    data = detail(client, batch)
    assert data['summary']['headline'] == '32 of 50 verified'

    async def executors(conn):
        rows = await conn.execute(text(
            "SELECT u.email, count(*) FROM platform_action_executions e JOIN users u ON u.id=e.executor_user_id "
            "WHERE e.duty='invoice.verify' AND e.status='verified' GROUP BY u.email"))
        return dict(rows.all())
    assert run_db(executors) == {FATIMA: 25, REHAN: 25}


def test_crash_mid_verification_resumes_without_rechecking(client, monkeypatch):
    import app.finance.handlers as handlers
    group, people, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    real, calls = handlers._verify_one, {'n': 0}

    async def crashing(ctx, batch_row, inv, ref):
        calls['n'] += 1
        if calls['n'] == 11:
            raise RuntimeError('worker died')
        return await real(ctx, batch_row, inv, ref)
    monkeypatch.setattr(handlers, '_verify_one', crashing)
    perform(client, group, run, step(client, batch, 'verify')['id'], FATIMA)
    pump(client, 1)
    verify = step(client, batch, 'verify')
    assert verify['phase'] == 'recover'
    assert sum(i['status'] != 'pending' for i in detail(client, batch)['invoices']) == 10
    monkeypatch.setattr(handlers, '_verify_one', real)

    # A worker that died holding the lease: the step is reclaimed after the lease expires.
    run_db(lambda conn: conn.execute(text(
        "UPDATE wf_steps SET status='running', claim_token=gen_random_uuid(), lease_until=now()-interval '1 second' "
        "WHERE node_id='verify'")))
    pump(client, 6)
    assert detail(client, batch)['summary']['headline'] == '32 of 50 verified'

    async def per_invoice(conn):
        rows = await conn.execute(text(
            "SELECT count(*), count(DISTINCT idempotency_key) FROM platform_action_executions "
            "WHERE duty='invoice.verify' AND status <> 'failed'"))
        return rows.one()
    total, distinct = run_db(per_invoice)
    assert total == distinct == 50  # every invoice decided exactly once


def test_payment_crash_after_dispatch_is_reconciled_without_second_submit(client, mock_bank):
    group, people, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    pump(client, 3)
    for approval in approvals(client):
        decide(client, approval)
    pump(client, 4)
    mock_bank.accept_then_fail = TimeoutError('response lost after the bank accepted')  # ambiguous submit
    mock_bank.fail_reads = 1  # and the first read-back is unavailable
    perform(client, group, run, step(client, batch, 'pay')['id'], TREASURY)
    pump(client, 8)
    data = detail(client, batch)
    assert {p['status'] for p in data['payments']} == {'verified'}
    assert mock_bank.submit_calls == 2  # one per payment, never twice
    assert len(mock_bank.transfers) == 2


def test_pause_stops_next_slice_and_resume_continues(client):
    group, people, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    perform(client, group, run, step(client, batch, 'verify')['id'], FATIMA)
    pump(client, 1)
    login(client, CLERK)  # the requester controls the run
    assert client.post(f'/api/workflow-engine/scopes/{group}/runs/{run}/control/pause').status_code == 200
    pump(client, 3)
    assert sum(i['status'] == 'pending' for i in detail(client, batch)['invoices']) == 25
    login(client, CLERK)
    assert client.post(f'/api/workflow-engine/scopes/{group}/runs/{run}/control/resume').status_code == 200
    pump(client, 4)
    assert detail(client, batch)['summary']['headline'] == '32 of 50 verified'


def test_department_scope_and_cross_tenant_lookups(client):
    group, people, _, _ = setup_finance(client)
    batch, ids, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_id = step(client, batch, 'verify')['id']
    # An ordinary member of this department without finance roles: forbidden, nothing revealed.
    login(client, USER2_EMAIL)
    assert client.get(f'/api/finance/batches/{batch}').status_code == 403
    assert client.get('/api/finance/batches').json()['batches'] == []
    perform(client, group, run, verify_id, USER2_EMAIL, expected=403)

    # A finance verifier of another department only: every lookup here is "not found".
    other, _ = seed_department('Other department')
    outsider = 'outsider@example.com'

    async def seed_outsider(conn):
        uid = uuid4()
        await conn.execute(text("INSERT INTO users(id,email,role,password_hash,is_active,auth_version) "
                                "VALUES (:id,:e,'user',:p,true,0)"), {'id': uid, 'e': outsider, 'p': _PASSWORD_HASH})
        await conn.execute(text('INSERT INTO access_group_members(group_id,user_id) VALUES (:g,:u)'), {'g': other, 'u': uid})
        from app.platform.sync import backfill_all
        await conn.run_sync(backfill_all)
        return str(uid)
    people[outsider] = run_db(seed_outsider)
    grant(client, other, people, outsider, 'finance.verifier')
    login(client, outsider)
    assert client.get(f'/api/finance/batches/{batch}').status_code == 404
    assert client.get('/api/finance/batches').json()['batches'] == []
    assert client.get('/api/finance/exceptions').json()['invoices'] == []
    assert client.get(f'/api/finance/scopes/{group}/vendors').status_code == 404
    assert client.post(f'/api/finance/invoices/{ids[0]}/resolve', json={'action': 'reject'}).status_code == 404
    perform(client, group, run, verify_id, outsider, expected=404)
    login(client, outsider)
    assert client.get('/api/finance/approvals').json()['approvals'] == []


def test_exception_correction_round_two(client):
    group, people, _, _ = setup_finance(client)
    batch, ids, expected = create_batch(client, group)
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    pump(client, 3)
    for approval in approvals(client):
        decide(client, approval, decision='reject')
    pump(client, 4)
    data = detail(client, batch)
    assert data['summary']['run']['status'] == 'completed' and data['batch']['status'] == 'completed'
    assert sum(i['status'] == 'exception' for i in data['invoices']) == 50  # rejected payments return to the queue
    # Correct one data exception (missing invoice number) and re-run: only pending invoices are checked again.
    missing = ids[[n for n, (_, r) in enumerate(expected) if r == 'missing_fields'][0]]
    login(client, CLERK)
    response = client.post(f'/api/finance/invoices/{missing}/resolve',
                           json={'action': 'correct', 'fields': {'invoice_number': 'INV-E01-FIXED'}, 'note': 'Supplier copy'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'pending' and response.json()['revision'] == 2
    run2 = start(client, batch)
    assert run2 != run
    pump(client, 2)
    verify_all(client, group, batch, run2, email=FATIMA)
    fixed = next(i for i in detail(client, batch)['invoices'] if i['id'] == missing)
    assert fixed['status'] == 'verified', fixed['reasons']
