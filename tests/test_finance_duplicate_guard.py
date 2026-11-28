"""Step R slice 5: duplicate-payment guard report (PostgreSQL *_test only, mock bank).

The guard reconciles connector transfers against FinPayment rows by read-back
only. It must never submit a transfer, create a payment, or change any row:
it reports mismatches and writes one audit event.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.finance.connectors import ConnectorUnavailable, TransferStatus
from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_finance_invoice_flow import (
    APPROVER,
    CLERK,
    TREASURY,
    approvals,
    create_batch,
    decide,
    detail,
    perform,
    pump,
    setup_finance,
    start,
    step,
    verify_all,
)


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override

    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def _drive_to_paid(client, group, batch):
    """Drive one batch to paid (2 verified payments, mock bank). Human start:
    the allow-list does not apply to a person starting a batch."""
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run)
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 2
    for approval in pending:
        decide(client, approval)
    pump(client, 4)
    perform(client, group, run, step(client, batch, 'pay')['id'], TREASURY)
    pump(client, 6)
    data = detail(client, batch)
    assert data['batch']['status'] == 'completed'
    assert {p['status'] for p in data['payments']} == {'verified'}
    return data['payments']


def _payments(batch):
    async def q(conn):
        rows = await conn.execute(text(
            "SELECT id, status, external_ref, amount, payee_account, currency, invoice_ids "
            "FROM fin_payments WHERE batch_id = :b ORDER BY created_at"), {'b': batch})
        return [dict(r._mapping) for r in rows]

    return run_db(q)


def run_guard(client, email=ADMIN_EMAIL, connector=None):
    """Run the guard as `email` through the app's own sessionmaker."""
    from app.core.config import get_settings
    from app.finance.duplicate_guard import run_duplicate_guard
    from app.main import app
    from app.db.models import User
    from sqlalchemy import select

    async def _run(sessionmaker):
        async with sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            return await run_duplicate_guard(db, user, get_settings(), cipher=app.state.cipher,
                                             connector=connector)

    return client.portal.call(_run, app.state.sessionmaker)


def _tamper_transfer(mock, ref, **changes):
    """Replace one stored mock transfer (test-only manipulation of the in-memory bank)."""
    old = mock.transfers[ref]
    mock.transfers[ref] = TransferStatus(
        ref, changes.get('status', old.status),
        changes.get('amount', old.amount), changes.get('currency', old.currency),
        changes.get('payee_account', old.payee_account), old.reference)


# --------------------------------------------------------------------------- reconciliation


def test_guard_all_matched(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    report = run_guard(client)
    assert report.checked == 2
    assert report.matched == 2
    assert report.mismatches == []
    assert report.unread == []
    assert report.has_problems is False
    assert mock_bank.submit_calls == 2  # the guard submitted nothing


def test_guard_amount_mismatch(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    pays = _payments(batch)
    _tamper_transfer(mock_bank, pays[0]['external_ref'], amount=Decimal('1.00'))
    report = run_guard(client)
    assert report.checked == 2 and report.matched == 1
    kinds = [(m.kind, m.payment_id) for m in report.mismatches]
    assert kinds == [('amount_mismatch', pays[0]['id'])]
    assert report.mismatches[0].detail['expected_amount'] == str(pays[0]['amount'])
    assert report.has_problems is True


def test_guard_missing_transfer(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    pays = _payments(batch)
    for pay in pays:
        del mock_bank.transfers[pay['external_ref']]
    report = run_guard(client)
    assert report.checked == 2 and report.matched == 0
    assert sorted(m.kind for m in report.mismatches) == ['missing_transfer', 'missing_transfer']
    assert {m.payment_id for m in report.mismatches} == {p['id'] for p in pays}


def test_guard_payee_mismatch(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    pays = _payments(batch)
    _tamper_transfer(mock_bank, pays[1]['external_ref'], payee_account='PK99ATTACKER0666')
    report = run_guard(client)
    assert [m.kind for m in report.mismatches] == ['payee_mismatch']
    assert report.mismatches[0].payment_id == pays[1]['id']


def test_guard_duplicate_payment(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    pays = _payments(batch)
    # A second dispatched payment row covering the very same invoices.
    import json
    import uuid

    async def insert(conn):
        await conn.execute(text(
            "INSERT INTO fin_payments(id, group_id, batch_id, round, vendor_id, amount, currency,"
            " payee_account, invoice_ids, status, created_at, updated_at)"
            " SELECT :id, group_id, batch_id, 2, vendor_id, amount, currency, payee_account,"
            " invoice_ids, 'dispatched', now(), now() FROM fin_payments WHERE id = :src"),
            {'id': uuid.uuid4(), 'src': pays[0]['id']})

    run_db(insert)
    report = run_guard(client)
    kinds = sorted(m.kind for m in report.mismatches)
    assert 'duplicate_payment' in kinds
    dup = next(m for m in report.mismatches if m.kind == 'duplicate_payment')
    assert set(dup.detail['shared_invoice_ids']) == set(map(str, pays[0]['invoice_ids']))
    assert len(dup.detail['payment_ids']) == 2


def test_guard_never_submits_and_changes_nothing(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    before = [(p['id'], p['status']) for p in _payments(batch)]
    report = run_guard(client)
    assert mock_bank.submit_calls == 2
    assert [(p['id'], p['status']) for p in _payments(batch)] == before
    assert report.checked == 2

    async def _audits(conn):
        rows = await conn.execute(text(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'finance.duplicate_guard'"))
        return rows.scalar()

    assert run_db(_audits) == 1  # exactly one audit event per run


def test_guard_ignores_unsubmitted_payments(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run)
    pump(client, 3)
    assert len(approvals(client)) == 2  # awaiting_approval: no transfer expected yet
    report = run_guard(client)
    assert report.checked == 0
    assert report.mismatches == [] and report.unread == []


def test_guard_unread_when_connector_fails(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    # The mock's `fail_reads` only gates `get()` — the Step E crash test depends
    # on `find()` succeeding while `get()` fails once, so the guard test injects
    # its own connector whose read-back (`find`) raises instead.
    class _FailingFindConnector:
        name = 'mock'

        def __init__(self, inner):
            self._inner = inner

        async def submit(self, req):
            return await self._inner.submit(req)

        async def get(self, ref):
            return await self._inner.get(ref)

        async def find(self, key):
            raise ConnectorUnavailable('read-back unavailable')

    report = run_guard(client, connector=_FailingFindConnector(mock_bank))
    assert report.checked == 2 and report.matched == 0
    assert report.mismatches == []
    assert len(report.unread) == 2
    assert all(u.reason == 'ConnectorUnavailable' for u in report.unread)


# --------------------------------------------------------------------------- nightly + API


def test_guard_nightly_runs_once_a_day(client, mock_bank):
    from app.core.config import get_settings
    from app.main import app
    from app.services.finance_autopilot import process_duplicate_guard

    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)

    async def _run(sessionmaker):
        return await process_duplicate_guard(sessionmaker, settings=get_settings(),
                                             cipher=app.state.cipher)

    first = client.portal.call(_run, app.state.sessionmaker)
    assert first is not None and first['checked'] == 2
    second = client.portal.call(_run, app.state.sessionmaker)
    assert second is None  # already ran today (UTC)


def test_guard_api_requires_admin(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    _drive_to_paid(client, group, batch)
    login(client, CLERK)
    response = client.get('/api/finance/duplicate-guard/report')
    assert response.status_code == 403, response.text
    login(client, ADMIN_EMAIL)
    response = client.get('/api/finance/duplicate-guard/report')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['checked'] == 2 and body['matched'] == 2 and body['mismatches'] == []
    assert body['has_problems'] is False
