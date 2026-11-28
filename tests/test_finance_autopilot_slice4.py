"""Step R slice 4: per-vendor autopilot allow-list (PostgreSQL *_test only, mock bank).

Only vendors an administrator marked autopilot_allowed may be auto-picked-up.
A batch naming a known but unlisted vendor — or no vendor in the master at
all — is left for a human to start. A new vendor is never allow-listed by
default, and the flag is set only through the vendor save endpoint.
"""

import os
import subprocess
import sys
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text

from app.finance.autopilot import AutopilotPolicy, disallowed_vendors
from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.finance_fixture import ACCOUNTS
from tests.test_finance_autopilot import allowlist_vendors, run_sweep
from tests.test_finance_invoice_flow import CLERK, create_batch, detail, setup_finance


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override

    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def allowlist_one(client, group, name):
    """Mark a single fixture vendor allow-listed."""
    login(client, ADMIN_EMAIL)
    response = client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': name, 'bank_account': ACCOUNTS[name], 'currency': 'PKR',
        'status': 'blocked' if 'Blocked' in name else 'active', 'autopilot_allowed': True})
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- sweep gate


def test_sweep_skips_batch_with_unlisted_vendors(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    # Nobody is allow-listed: the fixture batch names Alpha, Beta and Gamma.
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['skipped_vendor_not_allowed']
    assert 'allow-list' in decisions[0].reason
    assert detail(client, batch)['batch']['status'] == 'open'
    assert detail(client, batch)['summary']['run'] is None


def test_sweep_skips_mixed_batch(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_one(client, group, 'Alpha Supplies')  # Beta is known but unlisted
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['skipped_vendor_not_allowed']
    assert 'Beta Traders' in decisions[0].reason
    assert detail(client, batch)['batch']['status'] == 'open'


def test_sweep_starts_when_all_vendors_allowlisted(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['started']
    assert detail(client, batch)['batch']['status'] == 'verifying'


def test_sweep_skips_batch_naming_no_known_vendor(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    # A batch whose only invoice names a vendor that is not in the master:
    # a new vendor needs a human to start the batch.
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches',
                           json={'name': 'New vendor batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    batch = response.json()['id']
    response = client.post(f'/api/finance/batches/{batch}/invoices', json={'invoices': [
        {'invoice_number': 'INV-NEW-1', 'vendor_name': 'Brand New Vendor', 'po_number': 'PO-1001',
         'amount': '1000.00', 'currency': 'PKR', 'invoice_date': date.today().isoformat(),
         'department': 'Operations'}], 'texts': []})
    assert response.status_code == 201, response.text
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['skipped_vendor_not_allowed']
    assert 'new vendor' in decisions[0].reason
    assert detail(client, batch)['batch']['status'] == 'open'


def test_dry_run_skips_unlisted_vendor(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True, dry_run=True))
    assert [(d.action, d.dry_run) for d in decisions] == [('skipped_vendor_not_allowed', True)]
    assert detail(client, batch)['batch']['status'] == 'open'


def test_unknown_vendor_invoice_does_not_block_vetted_batch(client, mock_bank):
    # The fixture batch always contains one unknown-vendor invoice (INV-E03,
    # 'Unknown Imports'). With every master vendor allow-listed the sweep
    # still starts: verification rejects the unknown invoice and it never
    # reaches a payment — the allow-list gates vendors that can be paid.
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    decisions = run_sweep(client, CLERK, AutopilotPolicy(enabled=True))
    assert [d.action for d in decisions] == ['started']


# --------------------------------------------------------------------------- API + pure helper


def test_vendor_allowlist_toggle_via_api(client, mock_bank):
    group, _, _, vendors = setup_finance(client)
    login(client, ADMIN_EMAIL)
    rows = client.get(f'/api/finance/scopes/{group}/vendors').json()['vendors']
    assert all(v['autopilot_allowed'] is False for v in rows)  # default off, even for existing vendors
    response = client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': 'Alpha Supplies', 'bank_account': ACCOUNTS['Alpha Supplies'], 'currency': 'PKR',
        'autopilot_allowed': True})
    assert response.status_code == 201, response.text
    assert response.json()['autopilot_allowed'] is True
    rows = client.get(f'/api/finance/scopes/{group}/vendors').json()['vendors']
    alpha = next(v for v in rows if v['name'] == 'Alpha Supplies')
    assert alpha['autopilot_allowed'] is True
    assert alpha['id'] == vendors['Alpha Supplies']
    response = client.post(f'/api/finance/scopes/{group}/vendors', json={
        'name': 'Alpha Supplies', 'bank_account': ACCOUNTS['Alpha Supplies'], 'currency': 'PKR',
        'autopilot_allowed': False})
    assert response.status_code == 201, response.text
    assert response.json()['autopilot_allowed'] is False


def test_disallowed_vendors_is_pure():
    from app.finance.verification import name_key

    alpha, beta = UUID(int=1), UUID(int=2)
    by_name = {name_key('Beta Traders'): beta}
    # Verified invoices are judged by id; pending ones by name.
    rows = [SimpleNamespace(vendor_id=alpha, vendor_name='Alpha Supplies'),
            SimpleNamespace(vendor_id=None, vendor_name='Beta Traders'),
            SimpleNamespace(vendor_id=None, vendor_name='Unknown Imports'),
            SimpleNamespace(vendor_id=None, vendor_name=None)]
    # Unknown names do not block when a known vendor is present.
    assert disallowed_vendors(rows, {alpha, beta}, by_name) == ([], False)
    assert disallowed_vendors(rows, {alpha}, by_name) == ([beta], False)
    assert disallowed_vendors(rows, set(), by_name) == ([alpha, beta], False)
    # No invoice resolves to a vendor in the master: nothing vetted to pick up.
    assert disallowed_vendors(
        [SimpleNamespace(vendor_id=None, vendor_name='Brand New Vendor'),
         SimpleNamespace(vendor_id=None, vendor_name=None)], set(), {}) == ([], True)
    assert disallowed_vendors([], set(), {}) == ([], True)


# --------------------------------------------------------------------------- migration


def _alembic(*args):
    """Run alembic against the throwaway migration database. Never logs the URL."""
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"]).render_as_string(hide_password=False)
    env = dict(os.environ, DATABASE_URL=url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        capture_output=True, text=True, env=env, timeout=900)
    if result.returncode != 0:
        detail = "\n".join(line for line in (result.stdout + result.stderr).splitlines()
                           if "infraspine_agent76" not in line)
        raise AssertionError(f"alembic {' '.join(args)} failed:\n{detail}")
    return result


def _vendor_columns():
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"]).set(
        drivername="postgresql").render_as_string(hide_password=False)
    result = subprocess.run(
        ["psql", url, "-tAc",
         "SELECT column_name FROM information_schema.columns WHERE table_name = 'fin_vendors'"
         " AND column_name = 'autopilot_allowed'"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_migration_0105_roundtrip():
    """0104 -> 0105 -> 0104 -> 0105 on a throwaway database; `alembic check` clean at head."""
    _alembic("upgrade", "0104_finance_autopilot")
    assert _vendor_columns() == set()
    _alembic("upgrade", "head")
    assert _vendor_columns() == {"autopilot_allowed"}
    _alembic("check")
    _alembic("downgrade", "0104_finance_autopilot")
    assert _vendor_columns() == set()
    _alembic("upgrade", "head")
    assert _vendor_columns() == {"autopilot_allowed"}
