"""Step Y slice 4 (API-level): the anomaly queue's resolve actions and vendor history,
seeded directly in the database (PostgreSQL *_test only, mock bank).

The full-flow tests in tests/test_finance_anomaly_queue.py prove the same behaviour
end to end through verification; these tests seed exception invoices directly so the
new code paths can be exercised without the workflow pumps (which take ~10 minutes
per test on this VM and keep getting OOM-killed by sibling load).
"""

import json
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_finance_invoice_flow import (
    APPROVER,
    CLERK,
    FATIMA,
    approvals,
    grant,
    pump,
    seed_department,
    setup_finance,
    start,
    verify_all,
)

FLAG_REASONS = json.dumps([{'code': 'near_duplicate', 'message': 'Same vendor, amount and date',
                            'details': {'amount': '40000.00', 'prior_id': 'x'}}])
HOLD_REASONS = json.dumps([{'code': 'bank_account_changed', 'message': 'Bank account differs',
                            'details': {'previous_account': '********0001', 'new_account': '********0009'}},
                           {'code': 'payee_verification_hold', 'message': 'Payment held', 'details': {}}])
DETERMINISTIC_REASONS = json.dumps([{'code': 'po_missing', 'message': 'No purchase order number',
                                     'details': {}}])


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override
    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def seed_queue(client):
    """One department with roles, one vendor, one batch, three exception invoices:
    anomaly-flagged, hold-flagged, and deterministically rejected."""
    group, people = seed_department()
    grant(client, group, people, ADMIN_EMAIL, 'finance.admin')
    grant(client, group, people, CLERK, 'finance.clerk')
    grant(client, group, people, FATIMA, 'finance.verifier')
    grant(client, group, people, APPROVER, 'finance.approver')
    vendor_id, batch_id = uuid4(), uuid4()
    flagged_id, held_id, deterministic_id = uuid4(), uuid4(), uuid4()
    history_ids = [uuid4(), uuid4()]

    async def seed(conn):
        await conn.execute(text(
            "INSERT INTO fin_vendors(id, group_id, name, name_key, bank_account, currency, status) "
            "VALUES (:id, :g, 'Alpha Supplies', 'alpha supplies', 'PK00ALPHA0001', 'PKR', 'active')"),
            {'id': vendor_id, 'g': UUID(group)})
        await conn.execute(text(
            "INSERT INTO fin_invoice_batches(id, group_id, name, created_by, status, currency, tolerance_pct, tolerance_abs) "
            "VALUES (:id, :g, 'Seed batch', :u, 'open', 'PKR', 2.00, 0)"),
            {'id': batch_id, 'g': UUID(group), 'u': UUID(people[CLERK])})
        rows = [
            (history_ids[0], 'INV-H1', '40000.00', '2026-11-10', 'PK00ALPHA0001', 'verified', '[]'),
            (history_ids[1], 'INV-H2', '41000.00', '2026-11-15', 'PK00ALPHA0001', 'verified', '[]'),
            (flagged_id, 'INV-N1', '40000.00', '2026-11-18', 'PK00ALPHA0001', 'exception', FLAG_REASONS),
            (held_id, 'INV-N2', '42000.00', '2026-11-19', 'PK99NEW0009', 'exception', HOLD_REASONS),
            (deterministic_id, 'INV-D1', '43000.00', '2026-11-20', 'PK00ALPHA0001', 'exception',
             DETERMINISTIC_REASONS),
        ]
        for seq, (inv_id, number, amount, when, account, status, reasons) in enumerate(rows, start=1):
            await conn.execute(text(
                "INSERT INTO fin_invoices(id, group_id, batch_id, seq, invoice_number, vendor_name, vendor_id, "
                "po_number, amount, currency, invoice_date, bank_account_on_invoice, source_kind, source_digest, "
                "status, reasons) VALUES (:id, :g, :b, :seq, :n, 'Alpha Supplies', :v, 'PO-1001', :a, 'PKR', "
                ":d, :acct, 'json', :digest, :s, CAST(:reasons AS jsonb))"),
                {'id': inv_id, 'g': UUID(group), 'b': batch_id, 'seq': seq, 'n': number, 'v': vendor_id,
                 'a': amount, 'd': date.fromisoformat(when), 'acct': account, 'digest': 'a' * 64,
                 's': status, 'reasons': reasons})
    run_db(seed)
    return {'group': group, 'vendor_id': str(vendor_id), 'flagged': str(flagged_id),
            'held': str(held_id), 'deterministic': str(deterministic_id)}


def audit_for(invoice_id):
    async def read(conn):
        rows = (await conn.execute(text(
            "SELECT action, details FROM audit_logs WHERE target = :t ORDER BY id DESC"),
            {'t': str(invoice_id)})).all()
        return [{'action': a, 'details': d} for a, d in rows]
    return run_db(read)


def test_approve_anomaly_flagged_invoice_marks_verified_and_audits(client):
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{ids['flagged']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'Checked with the vendor'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'verified'
    assert response.json()['reasons'] == []
    assert response.json()['resolution_note'] == 'Checked with the vendor'
    events = [e for e in audit_for(ids['flagged']) if e['action'] == 'finance.invoice_anomaly_approved']
    assert len(events) == 1
    assert 'near_duplicate' in events[0]['details']['anomaly_flags']


def test_approve_held_invoice_releases_payee_hold(client):
    """An invoice returned to exceptions by payment grouping carries the hold marker
    alongside its anomaly flag — approving it still works and clears both."""
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{ids['held']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'New account confirmed'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'verified'
    assert response.json()['reasons'] == []
    events = [e for e in audit_for(ids['held']) if e['action'] == 'finance.invoice_anomaly_approved']
    assert len(events) == 1
    assert 'bank_account_changed' in events[0]['details']['anomaly_flags']


def test_approve_refused_when_deterministic_reason_present(client):
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{ids['deterministic']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'trying anyway'})
    assert response.status_code == 409, response.text
    assert not [e for e in audit_for(ids['deterministic'])
                if e['action'] == 'finance.invoice_anomaly_approved']


def test_approve_on_non_exception_invoice_conflicts(client):
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{ids['flagged']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'first'})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/finance/invoices/{ids['flagged']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'second'})
    assert response.status_code == 409, response.text


def test_approve_requires_verifier_permission(client):
    ids = seed_queue(client)
    login(client, CLERK)
    response = client.post(f"/api/finance/invoices/{ids['flagged']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'clerk attempt'})
    assert response.status_code == 403, response.text


def test_reject_of_flagged_invoice_audits_flag_codes(client):
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{ids['flagged']}/resolve",
                           json={'action': 'reject', 'fields': None, 'note': 'Duplicate of INV-H1'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'rejected'
    events = [e for e in audit_for(ids['flagged']) if e['action'] == 'finance.invoice_rejected']
    assert len(events) == 1
    assert 'near_duplicate' in events[0]['details']['anomaly_flags']


def test_approved_invoice_reaches_payment_grouping(client):
    """End to end: once the owner approves the flagged invoice, the batch's run
    groups it into a payment with the clean invoice — the payee hold is released.
    The flagged invoice is approved BEFORE the run starts, so grouping cannot
    race ahead of the approval (grouping only happens after verification)."""
    group, people, _, vendors = setup_finance(client)
    batch_id = uuid4()
    flagged_id, pending_id = uuid4(), uuid4()

    async def seed(conn):
        await conn.execute(text(
            "INSERT INTO fin_invoice_batches(id, group_id, name, created_by, status, currency, tolerance_pct, tolerance_abs) "
            "VALUES (:id, :g, 'Grouping batch', :u, 'open', 'PKR', 2.00, 0)"),
            {'id': batch_id, 'g': UUID(group), 'u': UUID(people[CLERK])})
        for seq, (inv_id, number, po, amount, when, status, reasons) in enumerate([
                (flagged_id, 'INV-N1', 'PO-1001', '40000.00', '2026-11-18', 'exception', FLAG_REASONS),
                (pending_id, 'INV-P1', 'PO-1003', '41000.00', '2026-11-19', 'pending', '[]')], start=1):
            await conn.execute(text(
                "INSERT INTO fin_invoices(id, group_id, batch_id, seq, invoice_number, vendor_name, vendor_id, "
                "po_number, amount, currency, invoice_date, bank_account_on_invoice, source_kind, source_digest, "
                "status, reasons) VALUES (:id, :g, :b, :seq, :n, 'Alpha Supplies', :v, :po, :a, 'PKR', "
                ":d, NULL, 'json', :digest, :s, CAST(:reasons AS jsonb))"),
                {'id': inv_id, 'g': UUID(group), 'b': batch_id, 'seq': seq, 'n': number,
                 'v': UUID(vendors['Alpha Supplies']), 'po': po, 'a': amount, 'd': date.fromisoformat(when),
                 'digest': 'a' * 64, 's': status, 'reasons': reasons})
    run_db(seed)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{flagged_id}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'Vendor confirmed'})
    assert response.status_code == 200, response.text
    run = start(client, str(batch_id))
    pump(client, 2)
    verify_all(client, group, str(batch_id), run)
    pump(client, 3)
    numbers = {n for p in approvals(client, APPROVER) for n in p['invoice_numbers']}
    assert 'INV-N1' in numbers
    assert 'INV-P1' in numbers


def test_vendor_history_masks_accounts_and_orders_newest_first(client):
    ids = seed_queue(client)
    login(client, FATIMA)
    response = client.get(f"/api/finance/scopes/{ids['group']}/vendors/{ids['vendor_id']}/history")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['vendor']['name'] == 'Alpha Supplies'
    numbers = [i['invoice_number'] for i in data['invoices']]
    assert numbers == ['INV-D1', 'INV-N2', 'INV-N1', 'INV-H2', 'INV-H1']  # newest first
    masked = {i['invoice_number']: i['bank_account_masked'] for i in data['invoices']}
    assert masked['INV-H1'] == '********0001'
    assert masked['INV-N2'] == '********0009'
    assert 'PK00ALPHA0001' not in response.text
    assert 'PK99NEW0009' not in response.text
    response = client.get(f"/api/finance/scopes/{ids['group']}/vendors/00000000-0000-0000-0000-000000000000/history")
    assert response.status_code == 404, response.text
    response = client.get('/api/finance/scopes/00000000-0000-0000-0000-000000000000'
                          f"/vendors/{ids['vendor_id']}/history")
    assert response.status_code == 404, response.text
