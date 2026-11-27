"""Read-only SQLite reporting connector acceptance on synthetic business rows."""

import asyncio
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import get_settings
from app.api.routes.rag import _spreadsheet_safe_value
from app.schemas.database_analysis import DatabaseAnalysisQuery
from app.services.database_analysis import (
    DatabaseAnalysisConfigurationError,
    DatabaseAnalysisQueryError,
    DepartmentScope,
    _catalog,
    _sqlite_readonly_authorizer,
    _sqlite_source_path,
    build_statement,
    catalog_response,
    run_analysis,
)
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


CATALOG = '{"main.orders":{"order_id":"integer","department":"text","region":"text","amount":"numeric"}}'


def _source_settings(path: Path, root: Path, *, postgres_url=None):
    return SimpleNamespace(
        database_url=SecretStr("postgresql+asyncpg://app:secret@localhost:5432/infraspine"),
        rag_database_analysis_url=SecretStr(postgres_url) if postgres_url else None,
        rag_database_analysis_sqlite_path=path,
        rag_database_analysis_sqlite_root=root,
        rag_database_analysis_catalog=CATALOG,
        rag_database_analysis_department_grants="",
        rag_database_analysis_source_label="Synthetic SQLite report",
        rag_database_analysis_statement_timeout_ms=5000,
        rag_database_analysis_max_rows=100,
    )


def _create_business_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, department TEXT, region TEXT, amount REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO orders (order_id, department, region, amount) VALUES (?, ?, ?, ?)",
            [
                (1, "finance", "north", 10.25),
                (2, "finance", "north", 20.50),
                (3, "sales", "south", 99.00),
                (4, "sales", "north", 7.00),
            ],
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sqlite_catalog_and_query_are_bounded_read_only_and_department_scoped(tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    source = root / "reporting.sqlite3"
    _create_business_db(source)
    settings = _source_settings(source, root)
    before = _sha256(source)

    catalog = catalog_response(settings)
    assert catalog.enabled is True
    assert catalog.source_label == "Synthetic SQLite report"
    assert [table.name for table in catalog.tables] == ["main.orders"]

    grouped = asyncio.run(
        run_analysis(
            settings,
            DatabaseAnalysisQuery(table="main.orders", operation="count", group_by=["region"], limit=1),
        )
    )
    assert grouped.rows == [["north", "3"]]
    assert grouped.truncated is True
    assert grouped.row_limit == 1
    assert "main.orders" in grouped.provenance

    scoped = asyncio.run(
        run_analysis(
            settings,
            DatabaseAnalysisQuery(
                table="main.orders",
                operation="sum",
                value="amount",
                filters=[{"column": "region", "operation": "contains", "value": "NORTH"}],
                limit=10,
            ),
            department_scopes=(DepartmentScope("main.orders", "department", "finance"),),
        )
    )
    assert scoped.rows == [["30.75"]]
    assert _sha256(source) == before

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        uri = source.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("INSERT INTO orders VALUES (4, 'finance', 'west', 5)")


def test_sqlite_policy_rejects_outside_root_dual_sources_and_mutating_authorizer_actions(tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    inside = root / "reporting.sqlite3"
    outside = tmp_path / "outside.sqlite3"
    _create_business_db(inside)
    _create_business_db(outside)

    with pytest.raises(DatabaseAnalysisConfigurationError, match="beneath"):
        _sqlite_source_path(_source_settings(outside, root))
    with pytest.raises(DatabaseAnalysisConfigurationError, match="one business reporting source"):
        catalog_response(_source_settings(inside, root, postgres_url="postgresql+asyncpg://reader:secret@erp.test:5432/erp"))
    assert _sqlite_readonly_authorizer(sqlite3.SQLITE_SELECT, None, None, "main", None) == sqlite3.SQLITE_OK
    assert _sqlite_readonly_authorizer(sqlite3.SQLITE_INSERT, "orders", None, "main", None) == sqlite3.SQLITE_DENY
    assert _sqlite_readonly_authorizer(sqlite3.SQLITE_ATTACH, "other.db", None, None, None) == sqlite3.SQLITE_DENY


def test_sqlite_statement_uses_bound_values_and_dialect_safe_selects():
    settings = _source_settings(Path("/unused"), Path("/unused"))
    table = _catalog(settings)["main.orders"]
    query = DatabaseAnalysisQuery(
        table="main.orders",
        operation="sum",
        value="amount",
        filters=[{"column": "region", "operation": "contains", "value": "north%_'; DELETE"}],
        limit=5,
    )
    statement, params, columns = build_statement(query, table, 5, dialect="sqlite")
    assert '"main"."orders"' in statement.text
    assert 'COLLATE NOCASE LIKE :filter_0 ESCAPE' in statement.text
    assert "ILIKE" not in statement.text and "::text" not in statement.text
    assert "DELETE" not in statement.text
    assert params["filter_0"] == "%north/%/_'; DELETE%"
    assert columns == ["sum"]
    with pytest.raises(DatabaseAnalysisQueryError, match="unsupported"):
        build_statement(query, table, 5, dialect="mysql")
    assert _spreadsheet_safe_value("  =HYPERLINK(\"https://invalid.test\")") == "'  =HYPERLINK(\"https://invalid.test\")"
    assert _spreadsheet_safe_value("-123.45") == "-123.45"
    assert _spreadsheet_safe_value("-cmd|' /C calc'!A0") == "'-cmd|' /C calc'!A0"


def test_sqlite_api_uses_admin_boundary_and_audits_query_shape_without_values(client, tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    source = root / "reporting.sqlite3"
    _create_business_db(source)
    configured = get_settings().model_copy(
        update={
            "rag_database_analysis_url": None,
            "rag_database_analysis_sqlite_path": source,
            "rag_database_analysis_sqlite_root": root,
            "rag_database_analysis_catalog": CATALOG,
            "rag_database_analysis_department_grants": "",
            "rag_database_analysis_source_label": "Synthetic SQLite report",
        }
    )
    overrides = client.app.dependency_overrides
    previous = overrides.get(get_settings)
    overrides[get_settings] = lambda: configured
    try:
        login(client, ADMIN_EMAIL)
        catalog = client.get("/api/rag/database-analysis/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["enabled"] is True
        assert catalog.json()["tables"][0]["name"] == "main.orders"

        secret_filter = "north-only-secret-filter"
        result = client.post(
            "/api/rag/database-analysis/query",
            json={
                "table": "main.orders",
                "operation": "sum",
                "value": "amount",
                "filters": [{"column": "region", "operation": "eq", "value": secret_filter}],
                "limit": 10,
            },
        )
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.json()["rows"] == [[""]]

        with sqlite3.connect(source) as connection:
            connection.execute("UPDATE orders SET region = '=1+1' WHERE order_id = 3")
        export = client.post(
            "/api/rag/database-analysis/export",
            json={"table": "main.orders", "operation": "rows", "limit": 10},
        )
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("text/csv")
        assert export.headers["content-disposition"].endswith('"business-analysis-main-orders.csv"')
        assert export.headers["cache-control"] == "no-store"
        assert export.headers["x-content-type-options"] == "nosniff"
        assert export.content.startswith(b"\xef\xbb\xbf")
        assert b"'=1+1" in export.content
        assert b"3,sales,'=1+1,99.0\r\n" in export.content

        async def read_audit(conn):
            return await conn.scalar(
                text(
                    "SELECT jsonb_agg(details)::text FROM audit_logs "
                    "WHERE action IN ('rag.database_analysis.executed', 'rag.database_analysis.exported')"
                )
            )

        audit_detail = run_db(read_audit)
        assert audit_detail and secret_filter not in audit_detail
        assert '"filter_columns": ["region"]' in audit_detail
        assert '"returned_rows": 4' in audit_detail

        login(client, USER_EMAIL)
        denied = client.post(
            "/api/rag/database-analysis/query",
            json={"table": "main.orders", "operation": "count", "limit": 10},
        )
        assert denied.status_code == 403
        denied_export = client.post(
            "/api/rag/database-analysis/export",
            json={"table": "main.orders", "operation": "count", "limit": 10},
        )
        assert denied_export.status_code == 403
    finally:
        if previous is None:
            overrides.pop(get_settings, None)
        else:
            overrides[get_settings] = previous


def test_sqlite_api_scopes_live_department_membership_and_revocation(client, tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    source = root / "reporting.sqlite3"
    _create_business_db(source)
    overrides = client.app.dependency_overrides
    previous = overrides.get(get_settings)

    login(client, ADMIN_EMAIL)
    group_response = client.post(
        "/api/admin/access/groups",
        json={"name": f"Finance reporting {uuid.uuid4().hex[:8]}", "kind": "department"},
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["id"]
    configured = get_settings().model_copy(
        update={
            "rag_database_analysis_url": None,
            "rag_database_analysis_sqlite_path": source,
            "rag_database_analysis_sqlite_root": root,
            "rag_database_analysis_catalog": CATALOG,
            "rag_database_analysis_department_grants": json.dumps(
                {group_id: [{"table": "main.orders", "column": "department", "value": "finance"}]}
            ),
            "rag_database_analysis_source_label": "Synthetic SQLite report",
        }
    )
    overrides[get_settings] = lambda: configured
    try:
        added = client.post(f"/api/admin/access/groups/{group_id}/members", json={"email": USER_EMAIL})
        assert added.status_code == 204, added.text

        login(client, USER_EMAIL)
        catalog = client.get("/api/rag/database-analysis/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["enabled"] is True
        assert [table["name"] for table in catalog.json()["tables"]] == ["main.orders"]

        scoped = client.post(
            "/api/rag/database-analysis/query",
            json={"table": "main.orders", "operation": "sum", "value": "amount", "group_by": ["department"], "limit": 10},
        )
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["rows"] == [["finance", "30.75"]]

        attempted_scope_escape = client.post(
            "/api/rag/database-analysis/query",
            json={
                "table": "main.orders",
                "operation": "count",
                "filters": [{"column": "department", "operation": "eq", "value": "sales"}],
                "limit": 10,
            },
        )
        assert attempted_scope_escape.status_code == 200, attempted_scope_escape.text
        assert attempted_scope_escape.json()["rows"] == [["0"]]

        login(client, ADMIN_EMAIL)
        employee_id = run_db(
            lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER_EMAIL})
        )
        removed = client.delete(f"/api/admin/access/groups/{group_id}/members/{employee_id}")
        assert removed.status_code == 204, removed.text

        login(client, USER_EMAIL)
        revoked_catalog = client.get("/api/rag/database-analysis/catalog")
        assert revoked_catalog.status_code == 200
        assert revoked_catalog.json()["enabled"] is False
        revoked_query = client.post(
            "/api/rag/database-analysis/query",
            json={"table": "main.orders", "operation": "count", "limit": 10},
        )
        assert revoked_query.status_code == 403
    finally:
        if previous is None:
            overrides.pop(get_settings, None)
        else:
            overrides[get_settings] = previous
