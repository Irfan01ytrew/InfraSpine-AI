"""Step Y slices 2–3: anomaly flags wired into finance.verify_invoices, and the
payee-verification hold at the payment boundary (PostgreSQL *_test only, mock bank).

Slice 2: flags from the department's prior batches are appended to the invoice
reasons and force the exceptions queue; a department's first batch has no baseline
and verifies normally (cold start). Slice 3: a bank account that differs from the
vendor's history holds the payment for owner verification — even when the vendor
master itself was changed, so deterministic check 15 passes; and a verified row
that still carries a hold code is refused by payment grouping.
"""

from datetime import date, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_finance_invoice_flow import (
    APPROVER,
    CLERK,
    approvals,
    decide,
    detail,
    pump,
    setup_finance,
    start,
    verify_all,
)

ANOMALY_CODES = {'exact_duplicate', 'near_duplicate', 'amount_outlier',
                 'first_time_payee', 'rapid_repeat_payments', 'bank_account_changed'}


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override
    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def row(number, vendor, po, amount, when, **extra):
    return {'invoice_number': number, 'vendor_name': vendor, 'po_number': po, 'amount': amount,
            'currency': 'PKR', 'invoice_date': when, 'department': 'Operations', **extra}


def create_small_batch(client, group, rows):
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches', json={'name': 'Anomaly batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    batch = response.json()['id']
    response = client.post(f'/api/finance/batches/{batch}/invoices', json={'invoices': rows, 'texts': []})
    assert response.status_code == 201, response.text
    ids = [item['id'] for item in response.json()['invoices']]
    assert len(ids) == len(rows)
    return batch, ids


def verify_batch(client, group, batch):
    run = start(client, batch)
    pump(client, 2)
    verify_all(client, group, batch, run)
    return detail(client, batch)['invoices']


def complete_run_by_rejecting(client, group, batch):
    """Drive the batch's run to completion by rejecting every payment approval.

    Needed because the workflow engine starves a second concurrent run of the same
    workflow: each pump tick claims the oldest due run, so a run waiting on approvals
    is re-claimed forever and a newer run's start node never executes (its verify
    step stays 'blocked'). Existing flow tests never run two batches in one test,
    so they never hit this; here every run must finish before the next starts.
    """
    pump(client, 3)
    for approval in approvals(client, APPROVER):
        decide(client, approval, decision='reject')
    pump(client, 4)
    data = detail(client, batch)
    assert data['batch']['status'] == 'completed', data['batch']
    return data


def codes(invoice):
    return [r['code'] for r in invoice['reasons']]


def by_number(invoices):
    return {i['invoice_number']: i for i in invoices}


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_first_batch_has_no_anomaly_baseline(client):
    """Cold start (slice 2): with no prior batch there is no history to compare against,
    so nothing is flagged — not even first-time payees. The Step E acceptance (clean
    invoices verify) is preserved for a department's first batch."""
    group, _, _, _ = setup_finance(client)
    batch, _ = create_small_batch(client, group, [
        row('INV-C1', 'Alpha Supplies', 'PO-1001', '40000.00', days_ago(10)),
        row('INV-C2', 'Beta Traders', 'PO-1002', '41000.00', days_ago(10)),
    ])
    for inv in verify_batch(client, group, batch):
        assert inv['status'] == 'verified', inv['reasons']
        assert inv['reasons'] == []


def test_anomaly_flags_route_flagged_invoices_to_exceptions(client):
    """Slice 2: against a prior batch, a near-duplicate and an amount outlier go to the
    exceptions queue with their flag codes in reasons; a clean invoice still verifies."""
    group, _, _, vendors = setup_finance(client)
    history_rows = [row(f'INV-H{n}', 'Alpha Supplies', f'PO-{1000 + 2 * n - 1}', '40000.00',
                        days_ago(60 - 10 * (n - 1))) for n in range(1, 7)]
    history_rows.append(row('INV-HB', 'Beta Traders', 'PO-1002', '41000.00', days_ago(10)))
    batch1, _ = create_small_batch(client, group, history_rows)
    first = verify_batch(client, group, batch1)
    assert all(i['status'] == 'verified' for i in first), [i['reasons'] for i in first]
    complete_run_by_rejecting(client, group, batch1)

    login(client, ADMIN_EMAIL)
    response = client.post(f'/api/finance/scopes/{group}/purchase-orders', json={
        'po_number': 'PO-9001', 'vendor_id': vendors['Alpha Supplies'], 'amount': '1000000.00',
        'currency': 'PKR', 'status': 'open'})
    assert response.status_code == 201, response.text
    response = client.post(f'/api/finance/scopes/{group}/receipts', json={
        'po_id': response.json()['id'], 'receipt_number': 'GRN-PO-9001', 'amount': '1000000.00',
        'received_on': days_ago(12)})
    assert response.status_code == 201, response.text

    batch2, _ = create_small_batch(client, group, [
        row('INV-N1', 'Alpha Supplies', 'PO-1013', '40000.00', days_ago(10)),   # near-duplicate
        row('INV-N2', 'Alpha Supplies', 'PO-9001', '400000.00', days_ago(5)),   # amount outlier
        row('INV-N3', 'Beta Traders', 'PO-1004', '42000.00', days_ago(5)),      # clean
    ])
    second = by_number(verify_batch(client, group, batch2))
    assert second['INV-N1']['status'] == 'exception'
    assert 'near_duplicate' in codes(second['INV-N1'])
    assert second['INV-N2']['status'] == 'exception'
    assert 'amount_outlier' in codes(second['INV-N2'])
    assert second['INV-N3']['status'] == 'verified', second['INV-N3']['reasons']
    assert not (set(codes(second['INV-N3'])) & ANOMALY_CODES)

    login(client, CLERK)
    queue = client.get('/api/finance/exceptions', params={'group_id': group}).json()['invoices']
    numbers = {i['invoice_number'] for i in queue}
    # The queue is group-wide: batch 1's invoices are here too (payment_rejected
    # after the run was closed by rejecting). The flagged pair must be present and
    # the clean invoice must not be.
    assert {'INV-N1', 'INV-N2'} <= numbers
    assert 'INV-N3' not in numbers


def test_bank_account_change_flags_even_when_master_changed(client):
    """Slice 3: the vendor master was updated to the new account, so deterministic check 15
    (bank_details_mismatch) passes — but the vendor's history used a different account, so
    the invoice is still held for owner verification. Full account numbers never appear
    in the recorded reasons."""
    group, _, _, _ = setup_finance(client)
    batch1, _ = create_small_batch(client, group, [
        row('INV-H1', 'Alpha Supplies', 'PO-1001', '40000.00', days_ago(10),
            bank_account_on_invoice='PK00ALPHA0001')])
    assert verify_batch(client, group, batch1)[0]['status'] == 'verified'
    complete_run_by_rejecting(client, group, batch1)

    login(client, ADMIN_EMAIL)
    assert client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': 'Alpha Supplies', 'bank_account': 'PK99NEW0009', 'currency': 'PKR'}).status_code == 201

    batch2, _ = create_small_batch(client, group, [
        row('INV-H2', 'Alpha Supplies', 'PO-1003', '41000.00', days_ago(5),
            bank_account_on_invoice='PK99NEW0009')])
    inv = verify_batch(client, group, batch2)[0]
    assert inv['status'] == 'exception'
    assert 'bank_account_changed' in codes(inv)
    assert 'bank_details_mismatch' not in codes(inv)
    assert 'PK00ALPHA0001' not in str(inv['reasons'])
    assert 'PK99NEW0009' not in str(inv['reasons'])


def test_payee_hold_refused_at_payment_grouping(client):
    """Slice 3, defence in depth: a row that is 'verified' but still carries a hold code
    (as if verified before Step Y) is refused by payment grouping — it returns to the
    exceptions queue with a payee_verification_hold reason, while a clean invoice in
    the same batch is grouped and approved normally. No payment ever contains the
    held invoice."""
    group, _, _, _ = setup_finance(client)
    # Batch A: history — one Alpha invoice, verified, run closed by rejecting its approval.
    batch_a, _ = create_small_batch(client, group, [
        row('INV-A1', 'Alpha Supplies', 'PO-1001', '40000.00', days_ago(10))])
    assert verify_batch(client, group, batch_a)[0]['status'] == 'verified'
    complete_run_by_rejecting(client, group, batch_a)

    # Batch B: one clean invoice (G1) and one missing-PO invoice (G2).
    batch_b, _ = create_small_batch(client, group, [
        row('INV-G1', 'Alpha Supplies', 'PO-1003', '41000.00', days_ago(5)),
        row('INV-G2', 'Alpha Supplies', None, '43000.00', days_ago(5)),
    ])
    got = by_number(verify_batch(client, group, batch_b))
    assert got['INV-G1']['status'] == 'verified', got['INV-G1']['reasons']
    assert got['INV-G2']['status'] == 'exception'
    assert 'po_missing' in codes(got['INV-G2'])
    complete_run_by_rejecting(client, group, batch_b)
    g1_id, g2_id = got['INV-G1']['id'], got['INV-G2']['id']

    # Simulate a row verified before Step Y: 'verified' with a hold code in reasons.
    run_db(lambda conn: conn.execute(text(
        "UPDATE fin_invoices SET status = 'verified', reasons = '[{\"code\": \"first_time_payee\", \"message\": "
        "\"Vendor has no previous invoice in this department\", \"details\": {}}]'::jsonb "
        "WHERE id = :i"), {'i': UUID(g1_id)}))
    # Correct G2 so the batch has a pending invoice and a second run can start
    # (start refuses a batch with nothing waiting for verification).
    login(client, CLERK)
    response = client.post(f'/api/finance/invoices/{g2_id}/resolve',
                           json={'action': 'correct', 'fields': {'po_number': 'PO-1005'}, 'note': 'PO added'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'pending'

    run2 = start(client, batch_b)
    pump(client, 2)
    verify_all(client, group, batch_b, run2)
    pump(client, 3)
    data = detail(client, batch_b)
    by_id = {i['id']: i for i in data['invoices']}
    assert by_id[g1_id]['status'] == 'exception'
    assert 'payee_verification_hold' in codes(by_id[g1_id])
    assert 'first_time_payee' in codes(by_id[g1_id])
    assert by_id[g2_id]['status'] == 'verified', by_id[g2_id]['reasons']
    # The held invoice is in no LIVE payment; exactly one live payment exists, for G2 only.
    # (Run 1's payment still lists G1, but it is 'rejected' — the hold only bars live payments.)
    assert all(g1_id not in p['invoice_ids'] for p in data['payments']
               if p['status'] == 'awaiting_approval')
    live = [p for p in data['payments'] if p['status'] == 'awaiting_approval']
    assert len(live) == 1 and live[0]['invoice_ids'] == [g2_id]
    pending = approvals(client, APPROVER)
    assert len(pending) == 1
