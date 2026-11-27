from types import SimpleNamespace
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.schemas.database_analysis import DatabaseAnalysisFilter, DatabaseAnalysisQuery
from app.services.database_analysis import (
    DatabaseAnalysisConfigurationError,
    DatabaseAnalysisQueryError,
    _catalog,
    _format_cell,
    _source_url,
    build_statement,
    catalog_response,
)


def settings(catalog=None, source_url="postgresql+asyncpg://reader:secret@erp.example.test:5432/erp"):
    return SimpleNamespace(
        database_url=SecretStr("postgresql+asyncpg://app:secret@postgres:5432/infraspine"),
        rag_database_analysis_url=SecretStr(source_url),
        rag_database_analysis_catalog=catalog or '{"erp.orders":{"id":"integer","region":"text","amount":"numeric"}}',
        rag_database_analysis_source_label="ERP reporting replica",
        rag_database_analysis_statement_timeout_ms=5000,
        rag_database_analysis_max_rows=100,
    )


def test_catalog_exposes_only_configured_schema_table_and_columns():
    result = catalog_response(settings())
    assert result.enabled is True
    assert result.source_label == "ERP reporting replica"
    assert [table.name for table in result.tables] == ["erp.orders"]
    assert [column.name for column in result.tables[0].columns] == ["id", "region", "amount"]


def test_structured_query_binds_filter_values_and_quotes_only_allowlisted_identifiers():
    configured = settings()
    table = _catalog(configured)["erp.orders"]
    query = DatabaseAnalysisQuery(
        table="erp.orders",
        operation="sum",
        value="amount",
        group_by=["region"],
        filters=[DatabaseAnalysisFilter(column="region", operation="contains", value="%_O'Reilly")],
        limit=10,
    )

    statement, params, columns = build_statement(query, table, 10)

    assert '"erp"."orders"' in statement.text
    assert '"region" ILIKE :filter_0 ESCAPE' in statement.text
    assert "O'Reilly" not in statement.text and "%_O'Reilly" not in statement.text
    assert params["filter_0"] == "%/%/_O'Reilly%"
    assert params["limit"] == 11
    assert params["cell_limit"] == 1001
    assert columns == ["region", "sum"]


def test_query_rejects_non_allowlisted_identifiers_and_unsafe_aggregates():
    table = _catalog(settings())["erp.orders"]
    injected = DatabaseAnalysisQuery(table="erp.orders; DROP TABLE users", operation="rows")
    with pytest.raises(DatabaseAnalysisQueryError, match="allowlist"):
        build_statement(injected, table, 10)

    text_sum = DatabaseAnalysisQuery(table="erp.orders", operation="sum", value="region")
    with pytest.raises(DatabaseAnalysisQueryError, match="numeric"):
        build_statement(text_sum, table, 10)

    unknown_column = DatabaseAnalysisQuery(
        table="erp.orders", operation="rows", filters=[{"column": "payroll", "operation": "eq", "value": "x"}]
    )
    with pytest.raises(DatabaseAnalysisQueryError, match="allowlist"):
        build_statement(unknown_column, table, 10)


def test_catalog_rejects_invalid_identifiers_types_and_application_database_alias():
    bad_identifier = settings('{"erp.orders; DROP TABLE x":{"amount":"numeric"}}')
    with pytest.raises(DatabaseAnalysisConfigurationError):
        _catalog(bad_identifier)

    bad_type = settings('{"erp.orders":{"amount":[]}}')
    with pytest.raises(DatabaseAnalysisConfigurationError):
        _catalog(bad_type)

    same_database = settings(source_url="postgresql+asyncpg://reader:secret@postgres:5432/infraspine")
    with pytest.raises(DatabaseAnalysisConfigurationError, match="separate"):
        _source_url(same_database)


def test_large_database_cells_are_bounded_for_text_and_decimal_values():
    text_value = _format_cell("x" * 5000)
    decimal_value = _format_cell(Decimal("9" * 5000))
    assert len(text_value) < 1024 and text_value.endswith("… [truncated]")
    assert len(decimal_value) < 1024 and decimal_value.endswith("… [truncated]")
