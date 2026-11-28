"""Step Y slice 5: per-department anomaly thresholds (PostgreSQL *_test only, mock bank).

Settings round-trip through the API, strict validation, finance.manage gating, and
the audit event for every change. The handler wiring (thresholds actually changing
detection) is proven at the helper level here; the full verification-path proof is
an owed quiet-VM run (see CHANGES-step-Y.md) because a full verification round
takes ~10 minutes per test on this VM.
"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.finance.anomaly import AnomalyConfig
from app.finance.handlers import anomaly_config_for
from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_finance_invoice_flow import CLERK, FATIMA, grant, seed_department

DEFAULTS = {'min_history': 5, 'outlier_mad_multiplier': '5', 'outlier_ratio_when_flat': '3',
            'near_duplicate_days': 7, 'velocity_window_days': 7, 'velocity_max_prior': 3}


@pytest.fixture(autouse=True)
def mock_bank():
    from app.finance.connectors import MOCK, set_connector_override
    MOCK.reset()
    set_connector_override(MOCK)
    yield MOCK
    set_connector_override(None)
    MOCK.reset()


def setup_group(client):
    group, people = seed_department()
    grant(client, group, people, ADMIN_EMAIL, 'finance.admin')
    grant(client, group, people, CLERK, 'finance.clerk')
    grant(client, group, people, FATIMA, 'finance.verifier')
    return group, people


def audit_for(target):
    async def read(conn):
        rows = (await conn.execute(text(
            "SELECT action, details FROM audit_logs WHERE target = :t ORDER BY id DESC"),
            {'t': str(target)})).all()
        return [{'action': a, 'details': d} for a, d in rows]
    return run_db(read)


def test_get_settings_returns_defaults_for_new_department(client):
    group, _ = setup_group(client)
    login(client, FATIMA)
    response = client.get(f'/api/finance/scopes/{group}/anomaly-settings')
    assert response.status_code == 200, response.text
    assert response.json()['thresholds'] == DEFAULTS
    assert response.json()['customized'] == []


def test_set_settings_persists_and_returns_effective_values(client):
    group, _ = setup_group(client)
    login(client, ADMIN_EMAIL)
    response = client.put(f'/api/finance/scopes/{group}/anomaly-settings',
                          json={'thresholds': {'velocity_max_prior': 10, 'near_duplicate_days': 3}})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['thresholds']['velocity_max_prior'] == 10
    assert data['thresholds']['near_duplicate_days'] == 3
    assert data['thresholds']['min_history'] == 5  # untouched fields keep defaults
    assert data['customized'] == ['near_duplicate_days', 'velocity_max_prior']
    login(client, FATIMA)
    again = client.get(f'/api/finance/scopes/{group}/anomaly-settings')
    assert again.json()['thresholds'] == data['thresholds']


def test_set_settings_rejects_invalid_values_and_changes_nothing(client):
    group, _ = setup_group(client)
    login(client, ADMIN_EMAIL)
    for bad in ({'min_history': 0}, {'min_history': 101}, {'outlier_mad_multiplier': 0},
                {'near_duplicate_days': 366}, {'velocity_window_days': 0}, {'unknown_key': 1}):
        response = client.put(f'/api/finance/scopes/{group}/anomaly-settings',
                              json={'thresholds': bad})
        assert response.status_code == 422, (bad, response.text)
    response = client.get(f'/api/finance/scopes/{group}/anomaly-settings')
    assert response.json()['thresholds'] == DEFAULTS
    assert response.json()['customized'] == []


def test_set_settings_requires_manage_permission(client):
    group, _ = setup_group(client)
    login(client, FATIMA)  # verifier: finance.read + invoice.verify, no finance.manage
    response = client.put(f'/api/finance/scopes/{group}/anomaly-settings',
                          json={'thresholds': {'velocity_max_prior': 10}})
    assert response.status_code == 403, response.text


def test_set_settings_writes_audit_event(client):
    group, _ = setup_group(client)
    login(client, ADMIN_EMAIL)
    response = client.put(f'/api/finance/scopes/{group}/anomaly-settings',
                          json={'thresholds': {'velocity_max_prior': 10}})
    assert response.status_code == 200, response.text
    events = [e for e in audit_for(group) if e['action'] == 'finance.anomaly_thresholds_changed']
    assert len(events) == 1
    assert events[0]['details']['changed'] == {'velocity_max_prior': 10}
    assert events[0]['details']['thresholds']['velocity_max_prior'] == 10


def test_handler_helper_reads_stored_settings(client):
    """The verification handler's config loader returns the stored overrides —
    this is the function VerifyInvoicesHandler.run calls once per slice."""
    group, _ = setup_group(client)
    login(client, ADMIN_EMAIL)
    response = client.put(f'/api/finance/scopes/{group}/anomaly-settings',
                          json={'thresholds': {'velocity_max_prior': 10, 'min_history': 2}})
    assert response.status_code == 200, response.text

    from app.main import app

    async def load(sessionmaker):
        async with sessionmaker() as session:
            return await anomaly_config_for(session, UUID(group))

    config = client.portal.call(load, app.state.sessionmaker)
    assert isinstance(config, AnomalyConfig)
    assert config.velocity_max_prior == 10
    assert config.min_history == 2
    assert config.near_duplicate_days == AnomalyConfig().near_duplicate_days
