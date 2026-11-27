"""Module 10 connector storage exists after migrations (0096) and stays tenant/owner scoped. Synthetic data only."""

from sqlalchemy import text

from tests.conftest import USER_EMAIL, login, run_db

TABLES = {"integration_connectors", "integration_jobs", "integration_records", "integration_events", "integration_oauth_attempts"}


def test_migrations_create_every_integration_table():
    rows = run_db(lambda c: c.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ))).all()
    assert TABLES <= {row.table_name for row in rows}


def test_connector_and_queued_job_persist_and_other_users_cannot_read_them(admin_client):
    tenant = admin_client.post("/api/company-graph/tenants", json={"name": "Synthetic Integrations"})
    assert tenant.status_code == 201, tenant.text
    tenant_id = tenant.json()["id"]
    created = admin_client.post(f"/api/integrations/{tenant_id}/connectors", json={
        "name": "Synthetic ERP", "provider": "rest", "base_url": "https://erp.example.com/api",
    })
    assert created.status_code == 201, created.text
    connector = created.json()["connector"]
    assert created.json()["webhook_secret"]
    job = admin_client.post(f"/api/integrations/{tenant_id}/connectors/{connector['id']}/jobs",
                            json={"operation": "verify", "idempotency_key": "synthetic-verify-0001"})
    assert job.status_code == 201, job.text
    again = admin_client.post(f"/api/integrations/{tenant_id}/connectors/{connector['id']}/jobs",
                              json={"operation": "verify", "idempotency_key": "synthetic-verify-0001"})
    assert again.status_code in (200, 201) and again.json()["id"] == job.json()["id"]  # idempotent enqueue
    overview = admin_client.get(f"/api/integrations/{tenant_id}/overview")
    assert overview.status_code == 200, overview.text
    stored = run_db(lambda c: c.execute(text("SELECT credentials::text AS c FROM integration_connectors"))).one()
    assert created.json()["webhook_secret"] not in stored.c  # credentials are sealed at rest

    admin_client.post("/api/auth/logout")
    login(admin_client, USER_EMAIL)
    outsider = admin_client.get(f"/api/integrations/{tenant_id}/overview")
    assert outsider.status_code in (403, 404)
