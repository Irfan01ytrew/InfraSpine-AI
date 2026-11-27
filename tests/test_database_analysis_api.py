from sqlalchemy import text

from app.api.routes import rag as rag_routes
from app.core.config import get_settings
from app.schemas.database_analysis import DatabaseAnalysisResult
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


def test_database_analysis_api_is_admin_only_returns_provenance_and_audits_no_filter_values(
    client, monkeypatch
):
    async def fake_analysis(_settings, query, department_scopes=None):
        if department_scopes is not None:
            from app.services.database_analysis import DatabaseAnalysisAccessDenied
            raise DatabaseAnalysisAccessDenied()
        assert query.table == "erp.orders"
        return DatabaseAnalysisResult(
            source_label="ERP reporting replica",
            table="erp.orders",
            retrieved_at="2026-10-26T12:00:00+00:00",
            operation="count",
            columns=["count"],
            rows=[["3"]],
            returned_rows=1,
            row_limit=10,
            truncated=False,
            provenance="Live query of ERP reporting replica · erp.orders",
        )

    monkeypatch.setattr(rag_routes, "run_database_analysis", fake_analysis)
    payload = {
        "table": "erp.orders",
        "operation": "count",
        "filters": [{"column": "region", "operation": "eq", "value": "sensitive-account-name"}],
        "limit": 10,
    }

    login(client, ADMIN_EMAIL)
    result = client.post("/api/rag/database-analysis/query", json=payload)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.json()["provenance"] == "Live query of ERP reporting replica · erp.orders"

    async def read_audit(conn):
        return await conn.scalar(
            text("SELECT details::text FROM audit_logs WHERE action = 'rag.database_analysis.executed'")
        )

    details = run_db(read_audit)
    assert details and "sensitive-account-name" not in details
    assert '"filter_columns": ["region"]' in details

    login(client, USER_EMAIL)
    catalog = client.get("/api/rag/database-analysis/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["enabled"] is False
    assert client.post("/api/rag/database-analysis/query", json=payload).status_code == 403


def test_database_analysis_catalog_discloses_disabled_state_without_secret_configuration(client):
    login(client, ADMIN_EMAIL)
    overrides = client.app.dependency_overrides
    prior = overrides.get(get_settings)
    disabled_settings = get_settings().model_copy(update={
        "rag_database_analysis_url": None,
        "rag_database_analysis_catalog": "",
    })
    overrides[get_settings] = lambda: disabled_settings
    try:
        catalog = client.get("/api/rag/database-analysis/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json() == {
            "enabled": False,
            "source_label": None,
            "tables": [],
            "message": "Business database analysis is not configured.",
        }
        disabled_query = client.post(
            "/api/rag/database-analysis/query",
            json={"table": "erp.orders", "operation": "count", "limit": 10},
        )
        assert disabled_query.status_code == 503
    finally:
        if prior is None:
            overrides.pop(get_settings, None)
        else:
            overrides[get_settings] = prior
