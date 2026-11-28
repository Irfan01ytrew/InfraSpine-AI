"""Step R slice 1: the finance autopilot sweep (PostgreSQL *_test only, mock bank).

The sweep picks up waiting invoice batches and wakes runs stalled on an expired
approval. It must never create a payment, approve anything, or call the bank:
those stay with the existing handlers and a human approver.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.models import User
from app.finance.autopilot import AutopilotPolicy, group_amounts, sweep
from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.finance_fixture import ACCOUNTS
from tests.test_finance_invoice_flow import (
    CLERK,
    FATIMA,
    approvals,
    create_batch,
    detail,
    pump,
    setup_finance,
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


def run_sweep(client, email, policy):
    """Run the sweep as `email` through the app's own sessionmaker."""
    from app.core.config import get_settings
    from app.main import app

    async def _run(sessionmaker):
        async with sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            return await sweep(db, user, get_settings(), policy, cipher=app.state.cipher)

    return client.portal.call(_run, app.state.sessionmaker)


def allowlist_vendors(client, group):
    """Mark every fixture vendor allow-listed (slice 4): the sweep only picks
    up batches whose vendors are all on the allow-list. The vendor save
    endpoint upserts by name, so re-posting flips the flag in place."""
    login(client, ADMIN_EMAIL)
    for name, account in ACCOUNTS.items():
        response = client.post(f'/api/finance/scopes/{group}/vendors', json={
            'name': name, 'bank_account': account, 'currency': 'PKR',
            'status': 'blocked' if 'Blocked' in name else 'active', 'autopilot_allowed': True})
        assert response.status_code == 201, response.text


def run_count(client, batch):
    """Workflow runs started for this batch (the trigger key is round-scoped)."""

    async def count(conn):
        row = await conn.execute(
            text("SELECT COUNT(*) FROM wf_runs WHERE idempotency_key = :k"),
            {'k': f'invoice-batch:{batch}:round:1'})
        return row.scalar()

    return run_db(count)


# --------------------------------------------------------------------------- policy gates


def test_autopilot_disabled_starts_nothing(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    assert run_sweep(client, CLERK, AutopilotPolicy(enabled=False)) == []
    assert detail(client, batch)['batch']['status'] == 'open'


def test_autopilot_dry_run_changes_nothing(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True, dry_run=True))
    assert [(d.action, d.dry_run) for d in decisions] == [('would_start', True)]
    assert detail(client, batch)['batch']['status'] == 'open'
    assert detail(client, batch)['summary']['run'] is None


def test_autopilot_picks_up_unstarted_batch_once(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['started']
    assert detail(client, batch)['batch']['status'] == 'verifying'
    # A second sweep must not start a second run for the same batch.
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['skipped_active']
    assert run_count(client, batch) == 1


# --------------------------------------------------------------------------- limits


def test_autopilot_skips_batch_over_payment_limit(client, mock_bank):
    group, _, _, vendors = setup_finance(client)
    batch, ids, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    # Seed verified-but-unpaid invoices directly (sweep-level state, not a full run):
    # seq 1,3,5 are Alpha invoices (40,100 + 40,300 + 40,500 = 120,900 PKR).
    alpha = vendors['Alpha Supplies']

    async def mark(conn):
        for seq in (1, 3, 5):
            await conn.execute(
                text("UPDATE fin_invoices SET status='verified', vendor_id=:v WHERE id=:i"),
                {'v': alpha, 'i': ids[seq - 1]})

    run_db(mark)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True, max_payment_amount=Decimal('1000')))
    assert [d.action for d in decisions] == ['skipped_over_limit']
    assert 'over max' in decisions[0].reason
    assert detail(client, batch)['batch']['status'] == 'open'
    assert detail(client, batch)['summary']['run'] is None
    # With a generous limit the same batch is picked up.
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True, max_payment_amount=Decimal('1000000')))
    assert [d.action for d in decisions] == ['started']


def test_group_amounts_is_pure_and_per_vendor():
    from types import SimpleNamespace

    rows = [SimpleNamespace(vendor_id='a', currency='PKR', amount=Decimal('10')),
            SimpleNamespace(vendor_id='a', currency='PKR', amount=Decimal('5')),
            SimpleNamespace(vendor_id='b', currency='PKR', amount=Decimal('7')),
            SimpleNamespace(vendor_id=None, currency='PKR', amount=Decimal('99'))]
    assert group_amounts(rows) == {('a', 'PKR'): Decimal('15'), ('b', 'PKR'): Decimal('7')}


# --------------------------------------------------------------------------- approvals


def _drive_to_approvals(client, group, batch):
    """Start via the sweep, verify, and pump until two approval requests exist."""
    allowlist_vendors(client, group)
    assert [d.action for d in run_sweep(client, CLERK, AutopilotPolicy(enabled=True))] == ['started']
    run = detail(client, batch)['summary']['run']['id']
    pump(client, 3)
    verify_all(client, group, batch, run)
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 2
    return pending


def test_autopilot_wakes_expired_approval_and_re_requests(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    pending = _drive_to_approvals(client, group, batch)
    old_id = pending[0]['approval_id']

    async def expire(conn):
        await conn.execute(text("UPDATE platform_approval_requests SET expires_at = now() - interval '1 hour' "
                                "WHERE id = :i"), {'i': old_id})

    run_db(expire)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['woken']
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 2
    assert old_id not in {a['approval_id'] for a in pending}  # a fresh request replaced it

    async def old_status(conn):
        row = await conn.execute(text("SELECT status FROM platform_approval_requests WHERE id = :i"), {'i': old_id})
        return row.scalar()

    assert run_db(old_status) == 'expired'
    # Nothing was paid while the approval was being re-requested.
    assert mock_bank.transfers == {}
    statuses = set()

    async def payment_statuses(conn):
        rows = await conn.execute(text("SELECT status FROM fin_payments WHERE batch_id = :b"), {'b': batch})
        statuses.update(r[0] for r in rows)

    run_db(payment_statuses)
    assert statuses == {'awaiting_approval'}


def test_autopilot_never_pays_without_approval_and_ignores_hostile_text(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, ids, _ = create_batch(client, group)
    pending = _drive_to_approvals(client, group, batch)
    # Payee accounts come from the vendor master only; the attacker's account is nowhere.
    assert {a['payee_account'] for a in pending} == {ACCOUNTS['Alpha Supplies'], ACCOUNTS['Beta Traders']}
    assert all('PK99ATTACKER0666' not in str(a) for a in pending)
    assert mock_bank.transfers == {}  # the sweep and the approval step submit no transfer
    data = detail(client, batch)
    by_id = {i['id']: i for i in data['invoices']}
    hostile = next(i for i in data['invoices'] if i['invoice_number'] == 'INV-X-666')
    assert hostile['status'] == 'exception'
    assert 'bank_details_mismatch' in [r['code'] for r in hostile['reasons']]
    assert hostile['payment_id'] is None  # never bound to a payment
    assert data['summary']['verified'] == 32
    assert data['summary']['headline'] == '32 of 50 verified'
