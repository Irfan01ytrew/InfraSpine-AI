"""Step AC slice 4: tax report (PostgreSQL *_test only, mock bank).

Totals by period + department + currency over verified/paid invoices, per
currency. Read-only: it totals DB rows, never pays, approves or changes a
payee. Permission-filtered via `readable_groups()` — another department is
invisible. The invoice schema has no separate expense-category column, so the
department is the breakdown dimension; no category is invented.
"""

from datetime import date, timedelta

from tests.conftest import ADMIN_EMAIL, login
from tests.test_finance_documents import mock_bank  # noqa: F401  (fixture reuse)
from tests.test_finance_invoice_flow import (
    APPROVER,
    CLERK,
    TREASURY,
    approvals,
    decide,
    detail,
    grant,
    perform,
    pump,
    seed_department,
    setup_finance,
    start,
    step,
    verify_all,
)

TODAY = date.today()
# A date safely inside the current month (org date is Asia/Karachi; the run can
# cross midnight, so expected month labels are always derived from these dates).
CURRENT = TODAY if TODAY.day <= 10 else TODAY - timedelta(days=10)
LAST_MONTH = date(TODAY.year, TODAY.month, 1) - timedelta(days=10)
CURRENT_MONTH = CURRENT.strftime('%Y-%m')
LAST_MONTH_LABEL = LAST_MONTH.strftime('%Y-%m')


def add_invoice_rows(client, batch, rows):
    login(client, CLERK)
    response = client.post(f'/api/finance/batches/{batch}/invoices', json={'invoices': rows})
    assert response.status_code == 201, response.text
    return response.json()['invoices']


def new_batch(client, group, currency='PKR'):
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches',
                           json={'name': 'Tax batch', 'currency': currency})
    assert response.status_code == 201, response.text
    return response.json()['id']


def add_usd_vendor(client, group):
    """A USD vendor + PO + goods receipt, mirroring setup_finance's PKR vendors."""
    login(client, ADMIN_EMAIL)
    response = client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': 'Delta Overseas', 'bank_account': 'US00DELTA0001', 'currency': 'USD', 'status': 'active'})
    assert response.status_code == 201, response.text
    vendor_id = response.json()['id']
    response = client.post(f'/api/finance/scopes/{group}/purchase-orders', json={
        'po_number': 'PO-9001', 'vendor_id': vendor_id, 'amount': '1000.00',
        'currency': 'USD', 'status': 'open'})
    assert response.status_code == 201, response.text
    po_id = response.json()['id']
    response = client.post(f'/api/finance/scopes/{group}/receipts', json={
        'po_id': po_id, 'receipt_number': 'GRN-PO-9001', 'amount': '1000.00',
        'received_on': (TODAY - timedelta(days=12)).isoformat()})
    assert response.status_code == 201, response.text


def seed_tax_data(client):
    """Two verified PKR invoices this month + one verified USD invoice this
    month + one verified PKR invoice last month. All verified, none paid."""
    group, _, _, _ = setup_finance(client)
    add_usd_vendor(client, group)
    batch = new_batch(client, group)
    add_invoice_rows(client, batch, [
        {'invoice_number': 'TX-001', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1001',
         'amount': '50000.00', 'currency': 'PKR', 'invoice_date': CURRENT.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'PK00ALPHA0001'},
        {'invoice_number': 'TX-002', 'vendor_name': 'Beta Traders', 'po_number': 'PO-1002',
         'amount': '50000.00', 'currency': 'PKR', 'invoice_date': CURRENT.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'PK00BETA0002'},
        {'invoice_number': 'TX-003', 'vendor_name': 'Delta Overseas', 'po_number': 'PO-9001',
         'amount': '1000.00', 'currency': 'USD', 'invoice_date': CURRENT.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'US00DELTA0001'},
        {'invoice_number': 'TX-004', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1003',
         'amount': '50000.00', 'currency': 'PKR', 'invoice_date': LAST_MONTH.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'PK00ALPHA0001'},
    ])
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run)
    by_number = {inv['invoice_number']: inv for inv in detail(client, batch)['invoices']}
    assert all(by_number[n]['status'] == 'verified' for n in ('TX-001', 'TX-002', 'TX-003', 'TX-004'))
    return group, batch, run


def pay_all(client, group, batch, run):
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 3  # one payment per vendor: Alpha, Beta, Delta
    for approval in pending:
        decide(client, approval)
    pump(client, 4)
    pay = step(client, batch, 'pay')
    assert pay['status'] == 'action'
    perform(client, group, run, pay['id'], TREASURY)
    pump(client, 6)
    assert detail(client, batch)['batch']['status'] == 'completed'
    assert all(i['status'] == 'paid' for i in detail(client, batch)['invoices'])


def tax_report(client, **params):
    login(client, CLERK)
    response = client.get('/api/finance/tax-report', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def row(body, month, currency):
    matches = [r for r in body['rows'] if r['month'] == month and r['currency'] == currency]
    assert len(matches) == 1, body['rows']
    return matches[0]


def test_tax_report_totals_by_period_and_currency(client, mock_bank):
    group, _, _ = seed_tax_data(client)
    body = tax_report(client, months=12)
    assert body['kind'] == 'tax_report'
    assert body['statuses'] == ['verified', 'paid']
    assert CURRENT_MONTH in body['months'] and LAST_MONTH_LABEL in body['months']
    current_pkr = row(body, CURRENT_MONTH, 'PKR')
    assert current_pkr['department'] == 'Operations'
    assert current_pkr['amount'] == '100000.00' and current_pkr['count'] == 2
    current_usd = row(body, CURRENT_MONTH, 'USD')
    assert current_usd['amount'] == '1000.00' and current_usd['count'] == 1
    last_pkr = row(body, LAST_MONTH_LABEL, 'PKR')
    assert last_pkr['amount'] == '50000.00' and last_pkr['count'] == 1
    totals = {t['currency']: t for t in body['totals']}
    assert totals['PKR'] == {'currency': 'PKR', 'amount': '150000.00', 'count': 3}
    assert totals['USD'] == {'currency': 'USD', 'amount': '1000.00', 'count': 1}
    assert body['warnings'] == []


def test_tax_report_paid_invoices_count(client, mock_bank):
    group, batch, run = seed_tax_data(client)
    pay_all(client, group, batch, run)
    body = tax_report(client, months=12)
    totals = {t['currency']: t for t in body['totals']}
    assert totals['PKR'] == {'currency': 'PKR', 'amount': '150000.00', 'count': 3}
    assert totals['USD'] == {'currency': 'USD', 'amount': '1000.00', 'count': 1}


def test_tax_report_pending_invoices_excluded(client, mock_bank):
    group, _, _ = seed_tax_data(client)
    # A second batch that is never started: its invoice stays `pending`.
    batch2 = new_batch(client, group)
    add_invoice_rows(client, batch2, [
        {'invoice_number': 'TX-900', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1001',
         'amount': '99999.00', 'currency': 'PKR', 'invoice_date': CURRENT.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'PK00ALPHA0001'}])
    body = tax_report(client, months=12)
    assert row(body, CURRENT_MONTH, 'PKR')['amount'] == '100000.00'  # the 99,999 pending one is absent


def test_tax_report_currency_filter(client, mock_bank):
    seed_tax_data(client)
    body = tax_report(client, months=12, currency='USD')
    assert [r['currency'] for r in body['rows']] == ['USD']
    assert body['totals'] == [{'currency': 'USD', 'amount': '1000.00', 'count': 1}]
    assert client.get('/api/finance/tax-report', params={'currency': 'PK'}).status_code == 422


def test_tax_report_csv_matches_json(client, mock_bank):
    seed_tax_data(client)
    login(client, CLERK)
    json_body = tax_report(client, months=12)
    response = client.get('/api/finance/tax-report.csv', params={'months': 12})
    assert response.status_code == 200, response.text
    assert 'text/csv' in response.headers['content-type']
    lines = response.text.strip().splitlines()
    assert lines[0] == 'month,department,currency,amount,count'
    assert len(lines) == 1 + len(json_body['rows'])
    csv_rows = [line.split(',') for line in lines[1:]]
    json_rows = [[r['month'], r['department'], r['currency'], r['amount'], str(r['count'])]
                 for r in json_body['rows']]
    assert sorted(csv_rows) == sorted(json_rows)


def test_tax_report_other_department_is_invisible(client, mock_bank):
    group, _, _ = seed_tax_data(client)
    # A second department the clerk cannot read, with its own verified invoice.
    group2, people2 = seed_department('Finance 2')
    grant(client, group2, people2, ADMIN_EMAIL, 'finance.admin')
    login(client, ADMIN_EMAIL)
    response = client.post(f'/api/finance/scopes/{group2}/batches',
                           json={'name': 'Other batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    batch2 = response.json()['id']
    response = client.post(f'/api/finance/batches/{batch2}/invoices', json={'invoices': [
        {'invoice_number': 'TX-901', 'vendor_name': 'Alpha Supplies', 'po_number': 'PO-1001',
         'amount': '999999.00', 'currency': 'PKR', 'invoice_date': CURRENT.isoformat(),
         'department': 'Operations', 'bank_account_on_invoice': 'PK00ALPHA0001'}]})
    assert response.status_code == 201, response.text
    body = tax_report(client, months=12)
    assert row(body, CURRENT_MONTH, 'PKR')['amount'] == '100000.00'  # the 999,999 invoice is invisible
