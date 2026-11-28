"""Step AC slice 3: cash-flow service + API (PostgreSQL *_test only, mock bank).

History comes from `spend()` (verified invoices only); pending invoices come from
the departments the caller can read. FinInvoice has no due-date column, so the
forecaster derives due dates (invoice_date + 30-day terms; overdue -> month 1).
The projection is read-only: it never pays, approves or changes a payee.
"""

from datetime import date, timedelta

import pytest

from tests.conftest import ADMIN_EMAIL, login
from tests.test_finance_documents import mock_bank  # noqa: F401  (fixture reuse)
from tests.test_finance_invoice_flow import (
    CLERK,
    FATIMA,
    detail,
    grant,
    pump,
    seed_department,
    setup_finance,
    start,
    verify_all,
)

TODAY = date.today()
PAST = (TODAY - timedelta(days=10)).isoformat()
OVERDUE = (TODAY - timedelta(days=40)).isoformat()


def add_invoice_rows(client, batch, rows):
    login(client, CLERK)
    response = client.post(f'/api/finance/batches/{batch}/invoices', json={'invoices': rows})
    assert response.status_code == 201, response.text
    return response.json()['invoices']


def new_batch(client, group, currency='PKR'):
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches',
                           json={'name': 'Cash-flow batch', 'currency': currency})
    assert response.status_code == 201, response.text
    return response.json()['id']


def seed_verified_and_pending(client):
    """Two verified 50,000 PKR invoices (history) + one overdue 25,000 PKR invoice
    from an unknown vendor (still pending -> scheduled in month 1)."""
    group, people, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    add_invoice_rows(client, batch, [
        {'invoice_number': 'CF-001', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1001',
         'amount': '50000.00', 'currency': 'PKR', 'invoice_date': PAST, 'department': 'Operations',
         'bank_account_on_invoice': 'PK00ALPHA0001'},
        {'invoice_number': 'CF-002', 'vendor_name': 'Beta Traders', 'po_number': 'PO-1002',
         'amount': '50000.00', 'currency': 'PKR', 'invoice_date': PAST, 'department': 'Operations',
         'bank_account_on_invoice': 'PK00BETA0002'},
        {'invoice_number': 'CF-003', 'vendor_name': 'Nobody Ltd', 'po_number': 'PO-1001',
         'amount': '25000.00', 'currency': 'PKR', 'invoice_date': OVERDUE, 'department': 'Operations',
         'bank_account_on_invoice': 'PK00NOBODY0009'},
    ])
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run, email=FATIMA)
    by_number = {inv['invoice_number']: inv for inv in detail(client, batch)['invoices']}
    assert by_number['CF-001']['status'] == 'verified'
    assert by_number['CF-002']['status'] == 'verified'
    assert by_number['CF-003']['status'] == 'exception'
    return group, people, batch


def test_cash_flow_projection(client, mock_bank):
    group, _, _ = seed_verified_and_pending(client)
    login(client, CLERK)
    response = client.get('/api/finance/cash-flow',
                          params={'currency': 'PKR', 'months': 3, 'opening_balance': '200000.00'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['kind'] == 'forecast'
    assert body['currency'] == 'PKR'
    # as_of is the organisation date (Asia/Karachi); derive the expected first
    # month from it instead of the test process date (a run can cross midnight).
    as_of = date.fromisoformat(body['as_of'])
    assert body['months'][0]['month'] == as_of.strftime('%Y-%m')
    assert body['history_months_used'] == 1
    assert body['opening_balance'] == '200000.00'
    months = body['months']
    assert len(months) == 3
    assert months[0]['month'] == as_of.strftime('%Y-%m')
    assert [m['month'] for m in months] == sorted(m['month'] for m in months)
    first = months[0]
    assert first['kind'] == 'forecast'
    assert first['scheduled_outflow'] == '25000.00'  # overdue invoice lands in month 1
    assert first['recurring_estimate'] == '100000.00'  # one history month: 2 x 50,000 verified
    assert first['expected_outflow'] == '125000.00'
    assert first['expected_inflow'] == '0.00'  # no inflow history is recorded anywhere
    assert first['closing_balance'] == '75000.00'
    assert first['confidence'] == 'low'  # only one history month
    assert any(w['code'] == 'no_inflow_history' for w in body['warnings'])
    # Month 2 carries the two verified invoices as scheduled: they are unpaid,
    # so per the plan's pending definition (pending / verified / exception /
    # approved) they fall due in month 2 (invoice date + 30-day terms crosses
    # into next month for this seed). The recurring baseline is the run-rate
    # for invoices not yet raised, so month 2 is 100,000 scheduled + 100,000
    # recurring.
    assert months[1]['scheduled_outflow'] == '100000.00'
    assert months[1]['recurring_estimate'] == '100000.00'
    assert months[1]['expected_outflow'] == '200000.00'
    assert months[2]['scheduled_outflow'] == '0.00'
    assert months[2]['expected_outflow'] == '100000.00'
    assert body['total_expected_outflow'] == '425000.00'  # 125,000 + 200,000 + 100,000
    assert body['closing_balance'] == '-225000.00'


def test_cash_flow_csv_matches_json(client, mock_bank):
    seed_verified_and_pending(client)
    login(client, CLERK)
    params = {'currency': 'PKR', 'months': 3, 'opening_balance': '200000.00'}
    json_body = client.get('/api/finance/cash-flow', params=params).json()
    response = client.get('/api/finance/cash-flow.csv', params=params)
    assert response.status_code == 200, response.text
    assert 'text/csv' in response.headers['content-type']
    lines = response.text.strip().splitlines()
    assert lines[0] == ('month,kind,scheduled_outflow,recurring_estimate,expected_outflow,expected_inflow,'
                        'net,closing_balance,basis,confidence,currency')
    assert len(lines) == 4  # header + 3 months
    first = lines[1].split(',')
    assert first[0] == json_body['months'][0]['month'] and first[1] == 'forecast'
    assert first[2] == '25000.00' and first[4] == '125000.00'
    assert first[7] == json_body['months'][0]['closing_balance']  # column 7 is closing_balance


def test_cash_flow_other_department_is_invisible(client, mock_bank):
    group, _, _ = seed_verified_and_pending(client)
    # A second department the clerk cannot read, with its own pending invoice.
    group2, people2 = seed_department('Finance 2')
    grant(client, group2, people2, ADMIN_EMAIL, 'finance.admin')
    login(client, ADMIN_EMAIL)
    response = client.post(f'/api/finance/scopes/{group2}/batches',
                           json={'name': 'Other batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    batch2 = response.json()['id']
    response = client.post(f'/api/finance/batches/{batch2}/invoices', json={'invoices': [
        {'invoice_number': 'CF-900', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1001',
         'amount': '999999.00', 'currency': 'PKR', 'invoice_date': OVERDUE, 'department': 'Operations',
         'bank_account_on_invoice': 'PK00ALPHA0001'}]})
    assert response.status_code == 201, response.text
    login(client, CLERK)
    body = client.get('/api/finance/cash-flow', params={'currency': 'PKR', 'months': 1}).json()
    assert body['months'][0]['scheduled_outflow'] == '25000.00'  # the 999,999 invoice is invisible


def test_cash_flow_empty_currency_and_bad_params(client, mock_bank):
    group, _, _ = seed_verified_and_pending(client)
    login(client, CLERK)
    body = client.get('/api/finance/cash-flow', params={'currency': 'USD', 'months': 2}).json()
    assert body['months'][0]['scheduled_outflow'] == '0.00'
    assert body['months'][0]['recurring_estimate'] == '0.00'
    assert body['history_months_used'] == 0
    assert any(w['code'] == 'no_history' for w in body['warnings'])
    assert client.get('/api/finance/cash-flow', params={'currency': 'PK', 'months': 3}).status_code == 422
    assert client.get('/api/finance/cash-flow', params={'currency': 'PKR', 'months': 13}).status_code == 422
    assert client.get('/api/finance/cash-flow', params={'currency': 'PKR', 'months': 3,
                                                        'opening_balance': '-1'}).status_code == 422
