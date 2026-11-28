"""Step R slices 2-3: worker wiring + org-settings policy + admin status UI (slice 2),
and approval reminders (slice 3). PostgreSQL *_test only, mock bank.

Slice 2 proves: the policy lives on org settings (disabled by default), an admin
can change it and see the last sweep, the worker pass starts waiting batches only
when enabled and only as a user who cannot approve or pay, and the migration
round-trips 0103 <-> 0104.
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.finance_fixture import ACCOUNTS
from tests.test_finance_invoice_flow import (
    APPROVER,
    CLERK,
    FATIMA,
    TREASURY,
    create_batch,
    detail,
    setup_finance,
)


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override

    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def run_autopilot_pass(client):
    """One worker pass through the app's own sessionmaker (as the worker would run it)."""
    from app.core.config import get_settings
    from app.main import app
    from app.services.finance_autopilot import process_finance_autopilot

    async def _run(sessionmaker):
        return await process_finance_autopilot(sessionmaker, settings=get_settings(),
                                               cipher=app.state.cipher)

    return client.portal.call(_run, app.state.sessionmaker)


def allowlist_vendors(client, group):
    """Mark every fixture vendor allow-listed (slice 4): the worker pass only
    picks up batches whose vendors are all on the allow-list."""
    login(client, ADMIN_EMAIL)
    for name, account in ACCOUNTS.items():
        response = client.post(f'/api/finance/scopes/{group}/vendors', json={
            'name': name, 'bank_account': account, 'currency': 'PKR',
            'status': 'blocked' if 'Blocked' in name else 'active', 'autopilot_allowed': True})
        assert response.status_code == 201, response.text


def get_status(client, email=ADMIN_EMAIL):
    login(client, email)
    response = client.get('/api/finance/autopilot/status')
    assert response.status_code == 200, response.text
    return response.json()


def put_policy(client, body, email=ADMIN_EMAIL, expected=200):
    login(client, email)
    response = client.put('/api/finance/autopilot/policy', json=body)
    assert response.status_code == expected, response.text
    return response


# --------------------------------------------------------------------------- policy

def test_policy_defaults_to_disabled(client):
    status = get_status(client)
    assert status['policy'] == {'enabled': False, 'dry_run': False,
                                'max_payment_amount': None, 'daily_cap': None}
    assert status['last_sweep'] is None


def test_policy_roundtrip_via_api(client):
    put_policy(client, {'enabled': True, 'dry_run': True,
                        'max_payment_amount': '500000.00', 'daily_cap': '2000000.00'})
    status = get_status(client)
    assert status['policy'] == {'enabled': True, 'dry_run': True,
                                'max_payment_amount': '500000.00', 'daily_cap': '2000000.00'}
    put_policy(client, {'enabled': False, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})
    assert get_status(client)['policy']['enabled'] is False


def test_policy_rejects_negative_amounts(client):
    put_policy(client, {'enabled': True, 'max_payment_amount': '-1'}, expected=422)
    put_policy(client, {'enabled': True, 'daily_cap': '-0.01'}, expected=422)
    assert get_status(client)['policy'] == {'enabled': False, 'dry_run': False,
                                            'max_payment_amount': None, 'daily_cap': None}


def test_policy_status_requires_admin(client):
    setup_finance(client)  # creates the clerk user used below
    login(client, CLERK)
    assert client.get('/api/finance/autopilot/status').status_code == 403
    assert client.put('/api/finance/autopilot/policy',
                      json={'enabled': True}).status_code == 403


def test_policy_change_is_audited(client):
    put_policy(client, {'enabled': True, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})

    async def _check(conn):
        row = await conn.execute(
            text("SELECT action, target FROM audit_logs WHERE action = 'finance.autopilot_policy_changed'"))
        return row.all()

    assert ('finance.autopilot_policy_changed', 'org_settings') in run_db(_check)
    # Leave the policy disabled again: later tests assume the default.
    put_policy(client, {'enabled': False, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})


# --------------------------------------------------------------------------- worker pass

def test_worker_pass_disabled_starts_nothing(client):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    put_policy(client, {'enabled': False, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})
    assert run_autopilot_pass(client) == 0
    assert detail(client, batch)['batch']['status'] == 'open'


def test_worker_pass_starts_batch_once(client):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    put_policy(client, {'enabled': True, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})
    decisions = run_autopilot_pass(client)
    assert decisions == 1
    assert detail(client, batch)['batch']['status'] == 'verifying'
    assert run_autopilot_pass(client) == 1  # second pass: same batch, now skipped_active
    status = get_status(client)
    assert status['last_sweep'] is not None
    actions = [d['action'] for d in status['last_sweep']['decisions']]
    # The last sweep is the second pass: it skipped the already-running batch, it did not start a duplicate run.
    assert actions == ['skipped_active']
    assert status['last_sweep']['actor_email'] in (ADMIN_EMAIL, CLERK)
    # And the earlier sweep is the one that started the batch: exactly one 'started' in the history.
    async def _history(conn):
        rows = await conn.execute(text(
            "SELECT details FROM audit_logs WHERE action = 'finance.autopilot_sweep' ORDER BY created_at"))
        return [r[0] for r in rows]

    history = run_db(_history)
    started = [d for event in history for d in event.get('decisions', []) if d.get('action') == 'started']
    assert len(started) == 1


def test_worker_pass_dry_run_starts_nothing(client):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    allowlist_vendors(client, group)
    put_policy(client, {'enabled': True, 'dry_run': True,
                        'max_payment_amount': None, 'daily_cap': None})
    assert run_autopilot_pass(client) == 1  # one would_start decision
    assert detail(client, batch)['batch']['status'] == 'open'
    assert get_status(client)['last_sweep'] is None  # dry runs write no audit event


def test_worker_pass_no_actor_records_audit(client):
    group, _, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    # Revoke every grant that carries invoice.ingest in this department.
    login(client, ADMIN_EMAIL)
    roles = client.get(f'/api/finance/scopes/{group}/roles').json()['roles']
    for row in roles:
        if row['email'] in (CLERK, ADMIN_EMAIL):
            response = client.delete(f"/api/finance/scopes/{group}/roles/{row['id']}")
            assert response.status_code == 204, response.text
    put_policy(client, {'enabled': True, 'dry_run': False,
                        'max_payment_amount': None, 'daily_cap': None})
    assert run_autopilot_pass(client) == 0
    # The clerk's role was revoked above, so read the batch as the verifier.
    assert detail(client, batch, email=FATIMA)['batch']['status'] == 'open'

    async def _check(conn):
        row = await conn.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action = 'finance.autopilot_no_actor'"))
        return row.scalar()

    assert run_db(_check) == 1


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


def _autopilot_columns():
    from sqlalchemy.engine import make_url

    # psql needs a plain postgresql:// URL; the SQLAlchemy asyncpg URL is not valid for it.
    url = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"]).set(
        drivername="postgresql").render_as_string(hide_password=False)
    result = subprocess.run(
        ["psql", url, "-tAc",
         "SELECT column_name FROM information_schema.columns WHERE table_name = 'org_settings'"
         " AND column_name LIKE 'finance_autopilot%' ORDER BY 1"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_migration_0104_roundtrip():
    """0103 -> 0104 -> 0103 -> 0104 on a throwaway database; `alembic check` clean at head."""
    _alembic("upgrade", "0103_unified_studio_governance")
    assert _autopilot_columns() == set()
    _alembic("upgrade", "head")
    assert _autopilot_columns() == {
        "finance_autopilot_enabled", "finance_autopilot_dry_run",
        "finance_autopilot_max_payment_amount", "finance_autopilot_daily_cap"}
    _alembic("check")
    _alembic("downgrade", "0103_unified_studio_governance")
    assert _autopilot_columns() == set()
    _alembic("upgrade", "head")
    assert len(_autopilot_columns()) == 4


def test_sweep_actor_never_holds_approve_or_transfer(client):
    """The worker actor may ingest, but must never be an approver or treasury user."""
    from app.main import app
    from app.services.finance_autopilot import sweep_actor

    setup_finance(client)

    async def _run(sessionmaker):
        async with sessionmaker() as db:
            actor = await sweep_actor(db)
            assert actor is not None
            return actor.email

    email = client.portal.call(_run, app.state.sessionmaker)
    assert email not in (APPROVER, TREASURY)


# --------------------------------------------------------------------------- approval reminders (slice 3)

def _awaiting_approval(client):
    """Drive one batch to awaiting_approval (2 pending payment approvals).

    Returns (group, people, batch, run, pending) where pending is the finance approvals list.
    """
    from tests.test_finance_invoice_flow import approvals, pump, start, verify_all

    group, people, _, _ = setup_finance(client)
    batch, _, _ = create_batch(client, group)
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run)
    pump(client, 3)
    pending = approvals(client)
    assert len(pending) == 2
    assert detail(client, batch)['batch']['status'] == 'awaiting_approval'
    return group, people, batch, run, pending


def run_reminders(client, **kwargs):
    """One reminder pass through the app's own sessionmaker (as the worker would run it)."""
    from app.finance.approval_reminders import process_finance_approval_reminders
    from app.main import app

    async def _run(sessionmaker):
        return await process_finance_approval_reminders(sessionmaker, **kwargs)

    return client.portal.call(_run, app.state.sessionmaker)


def _notices():
    async def _q(conn):
        rows = await conn.execute(text(
            "SELECT u.email, n.kind FROM wf_notices n JOIN users u ON u.id = n.recipient_id "
            "ORDER BY u.email, n.kind"))
        return [(r[0], r[1]) for r in rows]

    return run_db(_q)


def _reminder_audits():
    async def _q(conn):
        rows = await conn.execute(text(
            "SELECT target, details FROM audit_logs WHERE action = 'finance.approval_reminder' "
            "ORDER BY created_at"))
        return [(r[0], r[1]) for r in rows]

    return run_db(_q)


def test_reminder_nudges_approver_when_approval_created(client, mock_bank):
    _awaiting_approval(client)
    # Both approvals wait on the same workflow step, so the approver gets one notice for it.
    assert run_reminders(client) == 1
    assert _notices() == [(APPROVER, 'approval_required')]
    audits = _reminder_audits()
    assert len(audits) == 1
    assert audits[0][1]['kind'] == 'approval_required'
    assert audits[0][1]['recipient_count'] == 1


def test_reminder_is_idempotent(client, mock_bank):
    _awaiting_approval(client)
    assert run_reminders(client) == 1
    assert run_reminders(client) == 0
    assert _notices() == [(APPROVER, 'approval_required')]


def test_reminder_skips_requester_and_separated_duties(client, mock_bank):
    from tests.test_finance_invoice_flow import grant

    group, people, _, _, _ = _awaiting_approval(client)
    # Fatima verified this batch (invoice.verify) and the clerk requested the approvals:
    # neither may decide them, even after being granted the approver role.
    grant(client, group, people, FATIMA, 'finance.approver')
    grant(client, group, people, CLERK, 'finance.approver')
    assert run_reminders(client) == 1
    assert _notices() == [(APPROVER, 'approval_required')]


def test_reminder_expiring_soon_uses_expiring_kind(client, mock_bank):
    from datetime import datetime, timedelta

    _, _, _, _, pending = _awaiting_approval(client)
    assert run_reminders(client) == 1
    expires = datetime.fromisoformat(pending[0]['expires_at'])
    assert expires.tzinfo is not None
    # Three hours before expiry: inside the four-hour window, so a second, distinct nudge.
    assert run_reminders(client, now=expires - timedelta(hours=3)) == 1
    assert sorted(_notices()) == [(APPROVER, 'approval_expiring'), (APPROVER, 'approval_required')]
    # And the expiring nudge does not repeat either.
    assert run_reminders(client, now=expires - timedelta(hours=3)) == 0


def test_reminder_never_decides_or_changes_anything(client, mock_bank):
    from tests.test_finance_invoice_flow import approvals

    _, _, batch, _, pending = _awaiting_approval(client)
    before = {p['approval_id']: p['action_hash'] for p in pending}
    assert run_reminders(client) == 1
    assert {p['approval_id']: p['action_hash'] for p in approvals(client)} == before
    assert detail(client, batch)['batch']['status'] == 'awaiting_approval'
    assert mock_bank.submit_calls == 0


def test_reminder_skips_decided_approval(client, mock_bank):
    from tests.test_finance_invoice_flow import decide, pump

    _, _, _, _, pending = _awaiting_approval(client)
    decide(client, pending[0])
    pump(client, 2)
    assert run_reminders(client) == 1
    audits = _reminder_audits()
    assert len(audits) == 1 and audits[0][0] == pending[1]['approval_id']
    decide(client, pending[1])
    pump(client, 2)
    assert run_reminders(client) == 0
