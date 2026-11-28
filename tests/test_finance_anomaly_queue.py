"""Step Y slice 4: the anomaly queue — owner approve/reject of flagged invoices with a
reason, and the per-vendor history view (PostgreSQL *_test only, mock bank).

Approve accepts an anomaly-flagged invoice as-is (the owner reviewed the flags):
the invoice becomes verified, its reasons are cleared so payment grouping no longer
refuses it, and the decision is recorded in the append-only audit log with the flag
codes that were reviewed. Approve is refused when any deterministic reason is present
(the data is wrong — correct it instead), and it needs invoice.verify.
"""

import pytest
from sqlalchemy import text

from tests.conftest import login, run_db
from tests.test_finance_anomaly_wiring import (
    ANOMALY_CODES,
    by_number,
    codes,
    complete_run_by_rejecting,
    create_small_batch,
    days_ago,
    row,
    verify_batch,
)
from tests.test_finance_invoice_flow import CLERK, FATIMA, setup_finance


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override
    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def flagged_pair(client, group):
    """History batch (6 Alpha invoices) + a second batch with one near-duplicate
    and one clean invoice. Returns (batch2, flagged invoice, clean invoice)."""
    history_rows = [row(f'INV-H{n}', 'Alpha Supplies', f'PO-{1000 + 2 * n - 1}', '40000.00',
                      days_ago(60 - 10 * (n - 1))) for n in range(1, 7)]
    batch1, _ = create_small_batch(client, group, history_rows)
    assert all(i['status'] == 'verified' for i in verify_batch(client, group, batch1))
    complete_run_by_rejecting(client, group, batch1)
    batch2, _ = create_small_batch(client, group, [
        row('INV-N1', 'Alpha Supplies', 'PO-1013', '40000.00', days_ago(10)),  # near-duplicate
        # Clean: Alpha has history, so no first-time flag; 45000 matches no prior
        # amount (no near-duplicate) and is far below the flat-outlier bound.
        row('INV-N2', 'Alpha Supplies', 'PO-1015', '45000.00', days_ago(5)),
    ])
    second = by_number(verify_batch(client, group, batch2))
    assert second['INV-N1']['status'] == 'exception'
    assert 'near_duplicate' in codes(second['INV-N1'])
    assert second['INV-N2']['status'] == 'verified', second['INV-N2']['reasons']
    return batch2, second['INV-N1'], second['INV-N2']


def audit_for(invoice_id):
    async def read(conn):
        rows = (await conn.execute(text(
            "SELECT action, details FROM audit_logs WHERE target = :t ORDER BY id DESC"),
            {'t': str(invoice_id)})).all()
        return [{'action': a, 'details': d} for a, d in rows]
    return run_db(read)


def test_approve_anomaly_flagged_invoice_marks_verified_and_audits(client):
    """Approve: an anomaly-flagged invoice becomes verified with its reasons cleared,
    and the audit log records the reviewed flag codes."""
    group, _, _, _ = setup_finance(client)
    _, flagged, _ = flagged_pair(client, group)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{flagged['id']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'Checked with the vendor'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'verified'
    assert response.json()['reasons'] == []
    assert response.json()['resolution_note'] == 'Checked with the vendor'
    events = [e for e in audit_for(flagged['id']) if e['action'] == 'finance.invoice_anomaly_approved']
    assert len(events) == 1
    assert 'near_duplicate' in events[0]['details']['anomaly_flags']


def test_approve_refused_when_deterministic_reason_present(client):
    """Approve is only for advisory flags: an invoice rejected for a deterministic
    reason (missing PO) cannot be approved as-is — the data must be corrected."""
    group, _, _, _ = setup_finance(client)
    batch, _ = create_small_batch(client, group, [
        row('INV-D1', 'Alpha Supplies', None, '40000.00', days_ago(5))])
    inv = verify_batch(client, group, batch)[0]
    assert inv['status'] == 'exception'
    assert 'po_missing' in codes(inv)
    assert not (set(codes(inv)) & ANOMALY_CODES)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{inv['id']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'trying to approve anyway'})
    assert response.status_code == 409, response.text
    assert not [e for e in audit_for(inv['id']) if e['action'] == 'finance.invoice_anomaly_approved']


def test_approve_requires_verifier_permission(client):
    """A clerk (invoice.ingest, no invoice.verify) cannot approve a flagged invoice."""
    group, _, _, _ = setup_finance(client)
    _, flagged, _ = flagged_pair(client, group)
    login(client, CLERK)
    response = client.post(f"/api/finance/invoices/{flagged['id']}/resolve",
                           json={'action': 'approve', 'fields': None, 'note': 'clerk attempt'})
    assert response.status_code == 403, response.text


def test_reject_of_flagged_invoice_audits_flag_codes(client):
    """Reject still works for flagged invoices, and its audit event carries the flags."""
    group, _, _, _ = setup_finance(client)
    _, flagged, _ = flagged_pair(client, group)
    login(client, FATIMA)
    response = client.post(f"/api/finance/invoices/{flagged['id']}/resolve",
                           json={'action': 'reject', 'fields': None, 'note': 'Duplicate of INV-H3'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'rejected'
    events = [e for e in audit_for(flagged['id']) if e['action'] == 'finance.invoice_rejected']
    assert len(events) == 1
    assert 'near_duplicate' in events[0]['details']['anomaly_flags']


def test_vendor_history_masks_accounts_and_scopes_to_department(client):
    """Per-vendor history: prior invoices newest first, bank accounts masked to the
    last 4 characters — the full account never appears in the response."""
    group, _, _, vendors = setup_finance(client)
    batch1, _ = create_small_batch(client, group, [
        row('INV-H1', 'Alpha Supplies', 'PO-1001', '40000.00', days_ago(10),
            bank_account_on_invoice='PK00ALPHA0001'),
        row('INV-H2', 'Alpha Supplies', 'PO-1003', '41000.00', days_ago(5),
            bank_account_on_invoice='PK00ALPHA0001')])
    assert all(i['status'] == 'verified' for i in verify_batch(client, group, batch1))
    login(client, FATIMA)
    vendor_id = vendors['Alpha Supplies']
    response = client.get(f'/api/finance/scopes/{group}/vendors/{vendor_id}/history')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['vendor']['name'] == 'Alpha Supplies'
    numbers = [i['invoice_number'] for i in data['invoices']]
    assert numbers == ['INV-H2', 'INV-H1']  # newest first
    for item in data['invoices']:
        assert item['bank_account_masked'] == '********0001'
    assert 'PK00ALPHA0001' not in response.text
    # Unknown vendor, and a vendor id from no department at all, are both "not found".
    response = client.get(f'/api/finance/scopes/{group}/vendors/00000000-0000-0000-0000-000000000000/history')
    assert response.status_code == 404, response.text
    response = client.get('/api/finance/scopes/00000000-0000-0000-0000-000000000000'
                          f'/vendors/{vendor_id}/history')
    assert response.status_code == 404, response.text
