"""Acceptance against an explicitly configured, disposable SELECT-only PostgreSQL source."""

from sqlalchemy import text
from sqlalchemy.engine import make_url
import os
import pytest

from app.core.config import get_settings
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


_SYNTHETIC_SOURCE_URL = os.environ.get("RAG_DATABASE_ANALYSIS_TEST_URL")
pytestmark = pytest.mark.skipif(
    not _SYNTHETIC_SOURCE_URL,
    reason=(
        "requires an explicitly provisioned synthetic reporting database via "
        "RAG_DATABASE_ANALYSIS_TEST_URL; it must never query an operator/customer source"
    ),
)


def test_live_business_database_analysis_is_bounded_scoped_and_read_only(client):
    # Do not let a developer's .env silently turn this acceptance into queries against a live ERP.
    # The operator's source must explicitly match this test-only loopback *_test target.
    settings = get_settings()
    configured = settings.rag_database_analysis_url
    if configured is None:
        pytest.skip("no synthetic reporting source is configured for this test run")
    synthetic_url = make_url(_SYNTHETIC_SOURCE_URL)
    configured_url = make_url(configured.get_secret_value())
    if configured_url != synthetic_url:
        pytest.skip("configured reporting source does not match the explicit synthetic test target")
    if synthetic_url.host not in {"localhost", "127.0.0.1", "::1"} or not (synthetic_url.database or "").endswith("_test"):
        pytest.fail("RAG_DATABASE_ANALYSIS_TEST_URL must point to a loopback database whose name ends in _test")

    login(client, ADMIN_EMAIL)
    catalog = client.get("/api/rag/database-analysis/catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["enabled"] is True
    assert catalog.json()["source_label"] == "Synthetic ERP replica"
    assert [table["name"] for table in catalog.json()["tables"]] == ["erp.orders"]

    grouped = client.post("/api/rag/database-analysis/query", json={
        "table": "erp.orders",
        "operation": "count",
        "group_by": ["region"],
        "limit": 1,
    })
    assert grouped.status_code == 200, grouped.text
    assert grouped.json()["columns"] == ["region", "count"]
    assert grouped.json()["rows"] == [["north", "2"]]
    assert grouped.json()["row_limit"] == 1
    assert grouped.json()["truncated"] is True
    assert "erp.orders" in grouped.json()["provenance"]

    filtered = client.post("/api/rag/database-analysis/query", json={
        "table": "erp.orders",
        "operation": "sum",
        "value": "amount",
        "filters": [{"column": "region", "operation": "eq", "value": "north"}],
        "limit": 10,
    })
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["rows"] == [["30.75"]]

    injected_value = "north' OR 1=1 --"
    safe_filter = client.post("/api/rag/database-analysis/query", json={
        "table": "erp.orders",
        "operation": "count",
        "filters": [{"column": "region", "operation": "eq", "value": injected_value}],
        "limit": 10,
    })
    assert safe_filter.status_code == 200, safe_filter.text
    assert safe_filter.json()["rows"] == [["0"]]

    denied_table = client.post("/api/rag/database-analysis/query", json={
        "table": "public.users",
        "operation": "rows",
        "limit": 10,
    })
    assert denied_table.status_code == 422

    async def read_audit(conn):
        return await conn.scalar(text(
            "SELECT COALESCE(jsonb_agg(details), '[]'::jsonb)::text FROM audit_logs "
            "WHERE action = 'rag.database_analysis.executed'"
        ))

    audit_detail = run_db(read_audit)
    assert audit_detail and injected_value not in audit_detail

    login(client, USER_EMAIL)
    employee_catalog = client.get("/api/rag/database-analysis/catalog")
    assert employee_catalog.status_code == 200
    assert employee_catalog.json()["enabled"] is False
    assert client.post("/api/rag/database-analysis/query", json={
        "table": "erp.orders", "operation": "count", "limit": 10,
    }).status_code == 403
