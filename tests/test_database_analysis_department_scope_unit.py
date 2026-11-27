from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.api.routes.rag import _database_analysis_department_grants
from app.schemas.database_analysis import DatabaseAnalysisFilter, DatabaseAnalysisQuery
from app.services.database_analysis import (
    DatabaseAnalysisAccessDenied,
    DatabaseAnalysisConfigurationError,
    DepartmentScope,
    _catalog,
    build_statement,
    catalog_response,
)


DEPARTMENT_A = "00000000-0000-0000-0000-00000000000a"
DEPARTMENT_B = "00000000-0000-0000-0000-00000000000b"


def settings(grants=""):
    return SimpleNamespace(
        database_url=SecretStr("postgresql+asyncpg://app:secret@postgres:5432/infraspine"),
        rag_database_analysis_url=SecretStr("postgresql+asyncpg://reader:secret@erp.example.test:5432/erp"),
        rag_database_analysis_catalog=(
            '{"erp.orders":{"id":"integer","department":"text","region":"text","amount":"numeric"},'
            '"erp.invoices":{"id":"integer","department":"text","amount":"numeric"}}'
        ),
        rag_database_analysis_department_grants=grants,
        rag_database_analysis_source_label="Synthetic reporting replica",
        rag_database_analysis_statement_timeout_ms=5000,
        rag_database_analysis_max_rows=100,
    )


def test_server_scope_is_anded_with_user_filters_and_cannot_be_replaced():
    configured = settings()
    table = _catalog(configured)["erp.orders"]
    query = DatabaseAnalysisQuery(
        table="erp.orders",
        operation="sum",
        value="amount",
        filters=[DatabaseAnalysisFilter(column="region", operation="eq", value="south")],
        limit=10,
    )

    statement, params, _ = build_statement(
        query,
        table,
        10,
        [DepartmentScope(table="erp.orders", column="department", value="north")],
    )

    sql = str(statement)
    assert '"region" = :filter_0' in sql
    assert '"department" = :department_scope_0' in sql
    assert sql.index('"region" = :filter_0') < sql.index('"department" = :department_scope_0')
    assert params["filter_0"] == "south"
    assert params["department_scope_0"] == "north"


def test_multiple_department_grants_are_or_scoped_and_other_tables_are_denied():
    configured = settings()
    table = _catalog(configured)["erp.orders"]
    query = DatabaseAnalysisQuery(table="erp.orders", operation="count", limit=10)
    statement, params, _ = build_statement(
        query,
        table,
        10,
        [
            DepartmentScope(table="erp.orders", column="department", value="north"),
            DepartmentScope(table="erp.orders", column="department", value="south"),
        ],
    )
    assert '"department" = :department_scope_0 OR "department" = :department_scope_1' in str(statement)
    assert params["department_scope_0"] == "north"
    assert params["department_scope_1"] == "south"
    with pytest.raises(DatabaseAnalysisAccessDenied):
        build_statement(query, table, 10, [DepartmentScope("erp.invoices", "department", "north")])


def test_department_grants_are_validated_and_catalog_is_filtered_to_granted_tables():
    raw = (
        '{"' + DEPARTMENT_A + '":[{"table":"erp.orders","column":"department","value":"north"}],'
        '"' + DEPARTMENT_B + '":[{"table":"erp.invoices","column":"department","value":"south"}]}'
    )
    configured = settings(raw)
    table = _catalog(configured)["erp.orders"]
    result = catalog_response(configured, {"erp.orders"})
    assert [item.name for item in result.tables] == ["erp.orders"]
    no_access = catalog_response(configured, set())
    assert no_access.enabled is False
    assert no_access.tables == []
    _, params, _ = build_statement(
        DatabaseAnalysisQuery(table="erp.orders", operation="count", limit=10),
        table,
        10,
        [DepartmentScope("erp.orders", "department", "north")],
    )
    assert params["department_scope_0"] == "north"

    invalid_column = settings(
        '{"' + DEPARTMENT_A + '":[{"table":"erp.orders","column":"payroll","value":"north"}]}'
    )
    with pytest.raises(DatabaseAnalysisConfigurationError):
        catalog_response(invalid_column)
    malformed_uuid = settings('{"finance":[{"table":"erp.orders","column":"department","value":"north"}]}')
    with pytest.raises(DatabaseAnalysisConfigurationError):
        catalog_response(malformed_uuid)


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _MembershipDb:
    def __init__(self, *responses):
        self.responses = iter(responses)

    async def scalars(self, _statement):
        return _Scalars(next(self.responses))


def run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("The fake membership query unexpectedly suspended.")


def test_group_membership_and_manager_assignment_resolve_only_matching_department_grants():
    grants = (
        '{"' + DEPARTMENT_A + '":[{"table":"erp.orders","column":"department","value":"north"}],'
        '"' + DEPARTMENT_B + '":[{"table":"erp.orders","column":"department","value":"south"}]}'
    )
    configured = settings(grants)
    manager = SimpleNamespace(id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), role="manager")
    db = _MembershipDb([UUID(DEPARTMENT_A)], [UUID(DEPARTMENT_B)])

    resolved = run_immediate(_database_analysis_department_grants(db, manager, configured))

    assert {(item.group_id, item.value) for item in resolved} == {
        (DEPARTMENT_A, "north"),
        (DEPARTMENT_B, "south"),
    }


def test_employee_without_a_configured_department_grant_gets_no_scope():
    employee = SimpleNamespace(id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"), role="user")
    resolved = run_immediate(
        _database_analysis_department_grants(_MembershipDb([UUID(DEPARTMENT_A)]), employee, settings())
    )
    assert resolved == ()
