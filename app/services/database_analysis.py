"""Read-only, allowlisted queries against an operator-configured PostgreSQL source."""

from __future__ import annotations

import json
import asyncio
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.schemas.database_analysis import (
    DatabaseAnalysisCatalog,
    DatabaseAnalysisColumn,
    DatabaseAnalysisQuery,
    DatabaseAnalysisResult,
    DatabaseAnalysisTable,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_COLUMN_TYPES = {"text", "integer", "numeric", "date", "timestamp", "boolean"}
_SQL_TYPES = {
    "integer": "bigint",
    "numeric": "numeric",
    "date": "date",
    "timestamp": "timestamptz",
    "boolean": "boolean",
}
_NUMERIC_TYPES = {"integer", "numeric"}
_ORDERABLE_TYPES = _NUMERIC_TYPES | {"date", "timestamp"}
_MAX_TABLES = 20
_MAX_COLUMNS = 20
_MAX_CELL_CHARS = 1000


class DatabaseAnalysisConfigurationError(ValueError):
    """The operator supplied an invalid analysis source configuration."""


class DatabaseAnalysisQueryError(ValueError):
    """A structured query did not fit the server-owned catalog or policy."""


class DatabaseAnalysisUnavailable(RuntimeError):
    """The configured read-only source could not safely serve a query."""


class DatabaseAnalysisAccessDenied(PermissionError):
    """The caller has no operator-configured department grant for the requested source table."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Table:
    name: str
    schema: str
    table: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class DepartmentGrant:
    group_id: str
    table: str
    column: str
    value: str


@dataclass(frozen=True)
class DepartmentScope:
    table: str
    column: str
    value: str


def _identifier(value: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(value))


def _quoted(value: str) -> str:
    if not _identifier(value):
        raise DatabaseAnalysisConfigurationError("The configured database catalog contains an invalid identifier.")
    return f'"{value}"'


def _catalog(settings: Settings) -> dict[str, Table]:
    raw = settings.rag_database_analysis_catalog.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise DatabaseAnalysisConfigurationError("The configured database catalog is invalid.") from None
    if not isinstance(parsed, dict) or not 1 <= len(parsed) <= _MAX_TABLES:
        raise DatabaseAnalysisConfigurationError("The configured database catalog must list between 1 and 20 tables.")

    tables: dict[str, Table] = {}
    for name, raw_columns in parsed.items():
        if not isinstance(name, str) or name.count(".") != 1:
            raise DatabaseAnalysisConfigurationError("Every catalog table must use schema.table form.")
        schema_name, table_name = name.split(".")
        if not _identifier(schema_name) or not _identifier(table_name):
            raise DatabaseAnalysisConfigurationError("The configured database catalog contains an invalid table name.")
        if not isinstance(raw_columns, dict) or not 1 <= len(raw_columns) <= _MAX_COLUMNS:
            raise DatabaseAnalysisConfigurationError("Each catalog table must list between 1 and 20 columns.")
        columns = []
        for column_name, column_type in raw_columns.items():
            if not isinstance(column_name, str) or not _identifier(column_name):
                raise DatabaseAnalysisConfigurationError("The configured database catalog contains an invalid column name.")
            if not isinstance(column_type, str) or column_type not in _COLUMN_TYPES:
                raise DatabaseAnalysisConfigurationError("The configured database catalog contains an unsupported column type.")
            columns.append(Column(column_name, column_type))
        tables[name] = Table(name, schema_name, table_name, tuple(columns))
    return tables


def catalog_response(settings: Settings, allowed_tables: set[str] | None = None) -> DatabaseAnalysisCatalog:
    """Return only the configured, non-secret table and column allowlist."""
    has_url = bool(settings.rag_database_analysis_url and settings.rag_database_analysis_url.get_secret_value().strip())
    sqlite_path = getattr(settings, "rag_database_analysis_sqlite_path", None)
    has_sqlite = bool(sqlite_path and str(sqlite_path).strip())
    if has_url and has_sqlite:
        raise DatabaseAnalysisConfigurationError("Configure one business reporting source per deployment.")
    if not has_url and not has_sqlite and not settings.rag_database_analysis_catalog.strip():
        return DatabaseAnalysisCatalog(enabled=False, message="Business database analysis is not configured.")
    if (not has_url and not has_sqlite) or not settings.rag_database_analysis_catalog.strip():
        raise DatabaseAnalysisConfigurationError("A read-only database source and catalog must both be configured.")
    if has_sqlite:
        _sqlite_source_path(settings)
    tables = _catalog(settings)
    _department_grants(settings)  # malformed operator policy must fail closed before any query is offered
    if allowed_tables is not None:
        tables = {name: table for name, table in tables.items() if name in allowed_tables}
        if not tables:
            return DatabaseAnalysisCatalog(
                enabled=False,
                message="No configured reporting table is assigned to the departments you can access.",
            )
    return DatabaseAnalysisCatalog(
        enabled=True,
        source_label=settings.rag_database_analysis_source_label,
        tables=[
            DatabaseAnalysisTable(
                name=table.name,
                columns=[DatabaseAnalysisColumn(name=column.name, type=column.type) for column in table.columns],
            )
            for table in tables.values()
        ],
    )


def _department_grants(settings: Settings) -> dict[str, tuple[DepartmentGrant, ...]]:
    """Parse operator-owned per-department row grants and fail closed on malformed policy."""
    raw = getattr(settings, "rag_database_analysis_department_grants", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise DatabaseAnalysisConfigurationError("The department reporting grants are invalid.") from None
    if not isinstance(parsed, dict) or len(parsed) > 100:
        raise DatabaseAnalysisConfigurationError("Department reporting grants must be a bounded object.")

    tables = _catalog(settings)
    grants: dict[str, tuple[DepartmentGrant, ...]] = {}
    for group_id, raw_grants in parsed.items():
        if not isinstance(group_id, str):
            raise DatabaseAnalysisConfigurationError("A department reporting grant has an invalid group identifier.")
        try:
            normalized_group_id = str(uuid.UUID(group_id))
        except (ValueError, AttributeError):
            raise DatabaseAnalysisConfigurationError("A department reporting grant has an invalid group identifier.") from None
        if normalized_group_id != group_id.casefold():
            raise DatabaseAnalysisConfigurationError("A department reporting grant has a non-canonical group identifier.")
        if not isinstance(raw_grants, list) or not 1 <= len(raw_grants) <= 20:
            raise DatabaseAnalysisConfigurationError("Each department must have between 1 and 20 reporting grants.")
        department_grants = []
        for item in raw_grants:
            if not isinstance(item, dict) or set(item) != {"table", "column", "value"}:
                raise DatabaseAnalysisConfigurationError("A department reporting grant must specify table, column, and value.")
            table_name = item["table"]
            column_name = item["column"]
            value = item["value"]
            table = tables.get(table_name) if isinstance(table_name, str) else None
            if table is None or not isinstance(column_name, str) or not isinstance(value, str) or len(value) > 500:
                raise DatabaseAnalysisConfigurationError("A department reporting grant is outside the configured catalog.")
            column_type = next((column.type for column in table.columns if column.name == column_name), None)
            if column_type is None:
                raise DatabaseAnalysisConfigurationError("A department reporting grant is outside the configured catalog.")
            _normalize_filter(column_type, value)
            department_grants.append(DepartmentGrant(normalized_group_id, table_name, column_name, value))
        grants[normalized_group_id] = tuple(department_grants)
    return grants


def department_grants_for_groups(settings: Settings, group_ids: set[str]) -> tuple[DepartmentGrant, ...]:
    grants = _department_grants(settings)
    return tuple(grant for group_id in group_ids for grant in grants.get(group_id, ()))


def _source_url(settings: Settings):
    value: SecretStr | None = settings.rag_database_analysis_url
    if value is None or not value.get_secret_value().strip():
        raise DatabaseAnalysisUnavailable("The business database analysis source is not configured.")
    try:
        url = make_url(value.get_secret_value())
        application = make_url(settings.database_url.get_secret_value())
    except Exception:
        raise DatabaseAnalysisConfigurationError("A database URL is invalid.") from None
    if url.drivername != "postgresql+asyncpg" or not url.database:
        raise DatabaseAnalysisConfigurationError("The analysis source must be a PostgreSQL asyncpg URL.")
    if application.database and application.database.casefold() == url.database.casefold():
        raise DatabaseAnalysisConfigurationError("The analysis source must use a database separate from the application database.")
    return url


def _sqlite_source_path(settings: Settings):
    """Resolve the operator-configured SQLite file beneath its separately managed root."""
    configured = getattr(settings, "rag_database_analysis_sqlite_path", None)
    root_value = getattr(settings, "rag_database_analysis_sqlite_root", None)
    if not configured or not root_value:
        raise DatabaseAnalysisConfigurationError("The SQLite reporting path and root must both be configured.")
    source = configured if isinstance(configured, Path) else Path(configured)
    root = root_value if isinstance(root_value, Path) else Path(root_value)
    if source.is_symlink():
        raise DatabaseAnalysisConfigurationError("The SQLite reporting file cannot be a symbolic link.")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError, RuntimeError):
        raise DatabaseAnalysisConfigurationError("The SQLite reporting file must exist beneath its configured root.") from None
    if not resolved_root.is_dir() or not resolved_source.is_file():
        raise DatabaseAnalysisConfigurationError("The SQLite reporting path must name a file beneath a directory root.")

    try:
        application = make_url(settings.database_url.get_secret_value())
        if application.drivername.startswith("sqlite") and application.database not in {None, ":memory:"}:
            application_path = Path(application.database).resolve(strict=False)
            if application_path == resolved_source:
                raise DatabaseAnalysisConfigurationError("The analysis source must use a database separate from the application database.")
    except DatabaseAnalysisConfigurationError:
        raise
    except Exception:
        raise DatabaseAnalysisConfigurationError("A database URL is invalid.") from None
    return resolved_source


def _type_for(table: Table, name: str) -> str:
    for column in table.columns:
        if column.name == name:
            return column.type
    raise DatabaseAnalysisQueryError("A requested column is not in the configured allowlist.")


def _normalize_filter(column_type: str, value: str) -> Any:
    try:
        if column_type == "integer":
            if not re.fullmatch(r"[+-]?\d{1,18}", value):
                raise ValueError
            return int(value)
        if column_type == "numeric":
            if len(value) > 80:
                raise ValueError
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError
            return number
        if column_type == "date":
            return date.fromisoformat(value)
        if column_type == "timestamp":
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        if column_type == "boolean":
            if value.casefold() not in {"true", "false"}:
                raise ValueError
            return value.casefold() == "true"
        return value
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        raise DatabaseAnalysisQueryError("A filter value does not match the configured column type.") from None


def _validate_query(query: DatabaseAnalysisQuery, table: Table) -> None:
    columns = {column.name: column.type for column in table.columns}
    if query.operation == "rows":
        if query.value is not None or query.group_by:
            raise DatabaseAnalysisQueryError("Row queries cannot include an aggregate value or grouping.")
    elif query.operation == "count":
        if query.value is not None:
            raise DatabaseAnalysisQueryError("Count queries count rows and do not accept a value column.")
    elif query.value not in columns:
        raise DatabaseAnalysisQueryError("Choose an allowlisted value column for this operation.")
    if len(set(query.group_by)) != len(query.group_by):
        raise DatabaseAnalysisQueryError("Grouping columns must be unique.")
    if any(name not in columns for name in query.group_by):
        raise DatabaseAnalysisQueryError("A grouping column is not in the configured allowlist.")
    if query.operation in {"sum", "mean"} and columns[query.value] not in _NUMERIC_TYPES:
        raise DatabaseAnalysisQueryError("Sum and mean require a numeric allowlisted column.")
    for item in query.filters:
        column_type = columns.get(item.column)
        if column_type is None:
            raise DatabaseAnalysisQueryError("A filter column is not in the configured allowlist.")
        if item.operation == "contains" and column_type != "text":
            raise DatabaseAnalysisQueryError("Contains filters require a text column.")
        if item.operation in {"gt", "gte", "lt", "lte"} and column_type not in _ORDERABLE_TYPES:
            raise DatabaseAnalysisQueryError("Range filters require a numeric or date column.")
        _normalize_filter(column_type, item.value)


def build_statement(
    query: DatabaseAnalysisQuery,
    table: Table,
    limit: int,
    department_scopes: tuple[DepartmentScope, ...] | list[DepartmentScope] | None = None,
    *,
    dialect: str = "postgresql",
):
    """Build a parameterized SELECT; identifiers can only come from the validated catalog."""
    if query.table != table.name:
        raise DatabaseAnalysisQueryError("Choose a table from the configured allowlist.")
    _validate_query(query, table)
    if dialect not in {"postgresql", "sqlite"}:
        raise DatabaseAnalysisQueryError("The configured reporting database type is unsupported.")
    qualified = f"{_quoted(table.schema)}.{_quoted(table.table)}"
    columns = {column.name: column.type for column in table.columns}
    where = []
    params: dict[str, Any] = {"limit": limit + 1, "cell_limit": _MAX_CELL_CHARS + 1}
    for index, item in enumerate(query.filters):
        column_sql = _quoted(item.column)
        param = f"filter_{index}"
        value = _normalize_filter(columns[item.column], item.value)
        if item.operation == "contains":
            escaped = str(value).replace("/", "//").replace("%", "/%").replace("_", "/_")
            if dialect == "sqlite":
                where.append(f"{column_sql} COLLATE NOCASE LIKE :{param} ESCAPE '/' ")
            else:
                where.append(f"{column_sql} ILIKE :{param} ESCAPE '/' ")
            params[param] = f"%{escaped}%"
            continue
        cast = (
            {"integer": "INTEGER", "numeric": "REAL", "date": "TEXT", "timestamp": "TEXT", "boolean": "INTEGER"}.get(columns[item.column])
            if dialect == "sqlite"
            else _SQL_TYPES.get(columns[item.column])
        )
        right = f"CAST(:{param} AS {cast})" if cast else f":{param}"
        operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[item.operation]
        where.append(f"{column_sql} {operator} {right}")
        params[param] = str(value) if dialect == "sqlite" and isinstance(value, Decimal) else value

    if department_scopes is not None:
        matching_scopes = [scope for scope in department_scopes if scope.table == table.name]
        if not matching_scopes:
            raise DatabaseAnalysisAccessDenied("No department reporting grant covers the requested source table.")
        scope_terms = []
        for index, scope in enumerate(matching_scopes):
            column_type = _type_for(table, scope.column)
            param = f"department_scope_{index}"
            cast = _SQL_TYPES.get(column_type)
            right = f"CAST(:{param} AS {cast})" if cast else f":{param}"
            scope_terms.append(f"{_quoted(scope.column)} = {right}")
            scope_value = _normalize_filter(column_type, scope.value)
            params[param] = str(scope_value) if dialect == "sqlite" and isinstance(scope_value, Decimal) else scope_value
        where.append("(" + " OR ".join(scope_terms) + ")")

    where_sql = " WHERE " + " AND ".join(where) if where else ""
    if query.operation == "rows":
        if dialect == "sqlite":
            selection = ", ".join(
                f"SUBSTR(CAST({_quoted(column.name)} AS TEXT), 1, :cell_limit) AS {_quoted(column.name)}"
                for column in table.columns
            )
        else:
            selection = ", ".join(
                f"LEFT({_quoted(column.name)}::text, :cell_limit) AS {_quoted(column.name)}"
                for column in table.columns
            )
        statement = f"SELECT {selection} FROM {qualified}{where_sql} LIMIT :limit"
        result_columns = [column.name for column in table.columns]
    else:
        group_sql = ", ".join(_quoted(name) for name in query.group_by)
        if dialect == "sqlite":
            group_selection = ", ".join(
                f"SUBSTR(CAST({_quoted(name)} AS TEXT), 1, :cell_limit) AS {_quoted(name)}"
                for name in query.group_by
            )
        else:
            group_selection = ", ".join(
                f"LEFT({_quoted(name)}::text, :cell_limit) AS {_quoted(name)}" for name in query.group_by
            )
        group_selection = group_selection + ", " if group_selection else ""
        if query.operation == "count":
            aggregate = "COUNT(*)"
            result_name = "count"
        else:
            column_sql = _quoted(query.value or "")
            aggregate = {
                "sum": f"SUM({column_sql})",
                "mean": f"AVG({column_sql})",
                "min": f"MIN({column_sql})",
                "max": f"MAX({column_sql})",
                "count_distinct": f"COUNT(DISTINCT {column_sql})",
            }[query.operation]
            result_name = query.operation
        aggregate_selection = (
            f"SUBSTR(CAST(({aggregate}) AS TEXT), 1, :cell_limit)"
            if dialect == "sqlite"
            else f"LEFT(({aggregate})::text, :cell_limit)"
        )
        statement = f"SELECT {group_selection}{aggregate_selection} AS \"metric_value\" FROM {qualified}{where_sql}"
        if group_sql:
            statement += f" GROUP BY {group_sql} ORDER BY \"metric_value\" DESC"
            statement += " LIMIT :limit"
        result_columns = [*query.group_by, result_name]
    return text(statement), params, result_columns


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        rendered = value.isoformat()
    elif isinstance(value, Decimal):
        rendered = format(value, "f")
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    if len(rendered) > _MAX_CELL_CHARS:
        return rendered[:_MAX_CELL_CHARS] + "… [truncated]"
    return rendered


_SQLITE_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}


def _sqlite_readonly_authorizer(action: int, first: str | None, second: str | None, _database: str | None, _trigger: str | None) -> int:
    """Allow only reads and generated SELECT functions; deny writes, schema changes, and attach."""
    if action in _SQLITE_READ_ACTIONS:
        if action == sqlite3.SQLITE_FUNCTION and (second or first or "").casefold() == "load_extension":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _execute_sqlite_readonly(
    path: Path,
    statement: str,
    params: dict[str, Any],
    limit: int,
    timeout_ms: int,
) -> tuple[list[list[str]], bool]:
    """Run the generated query against an immutable URI connection with an execution budget."""
    deadline = time.monotonic() + timeout_ms / 1000
    connection = None
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=min(10, max(1, timeout_ms // 1000)),
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise DatabaseAnalysisUnavailable("The SQLite source did not confirm read-only mode.")
        databases = connection.execute("PRAGMA database_list").fetchall()
        if len(databases) != 1 or Path(databases[0][2]).resolve(strict=True) != path:
            raise DatabaseAnalysisUnavailable("The SQLite source did not confirm its configured file identity.")
        connection.execute("BEGIN")
        connection.set_authorizer(_sqlite_readonly_authorizer)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        records = connection.execute(statement, params).fetchmany(limit + 1)
        rows = [[_format_cell(value) for value in record] for record in records[:limit]]
        return rows, len(records) > limit
    except DatabaseAnalysisUnavailable:
        raise
    except sqlite3.Error:
        if time.monotonic() >= deadline:
            raise DatabaseAnalysisUnavailable("The SQLite query exceeded its configured execution limit.") from None
        raise DatabaseAnalysisUnavailable("The configured SQLite reporting file could not complete this read-only query.") from None
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()


async def run_analysis(
    settings: Settings,
    query: DatabaseAnalysisQuery,
    department_scopes: tuple[DepartmentScope, ...] | list[DepartmentScope] | None = None,
) -> DatabaseAnalysisResult:
    """Execute one bounded read-only transaction and return its source provenance."""
    try:
        catalog_response(settings)  # validate operator catalog before connecting
        sqlite_configured = bool(getattr(settings, "rag_database_analysis_sqlite_path", None))
        source_path = _sqlite_source_path(settings) if sqlite_configured else None
        url = None if sqlite_configured else _source_url(settings)
        table = _catalog(settings).get(query.table)
        if table is None:
            raise DatabaseAnalysisQueryError("Choose a table from the configured allowlist.")
        limit = min(query.limit, settings.rag_database_analysis_max_rows)
        dialect = "sqlite" if sqlite_configured else "postgresql"
        statement, params, result_columns = build_statement(query, table, limit, department_scopes, dialect=dialect)
        if source_path is not None:
            rows, truncated = await asyncio.to_thread(
                _execute_sqlite_readonly,
                source_path,
                statement.text,
                params,
                limit,
                settings.rag_database_analysis_statement_timeout_ms,
            )
        else:
            engine = create_async_engine(
                url,
                poolclass=NullPool,
                connect_args={
                    "timeout": min(10, max(1, settings.rag_database_analysis_statement_timeout_ms // 1000)),
                    "command_timeout": max(1, settings.rag_database_analysis_statement_timeout_ms // 1000),
                },
            )
            try:
                async with engine.connect() as connection:
                    async with connection.begin():
                        await connection.execute(text("SET TRANSACTION READ ONLY"))
                        transaction_read_only = await connection.scalar(text("SELECT current_setting('transaction_read_only')"))
                        if transaction_read_only != "on":
                            raise DatabaseAnalysisUnavailable("The database did not confirm a read-only transaction.")
                        await connection.execute(
                            text("SELECT set_config('statement_timeout', :timeout, true)"),
                            {"timeout": f"{settings.rag_database_analysis_statement_timeout_ms}ms"},
                        )
                        relation = f'{_quoted(table.schema)}.{_quoted(table.table)}'
                        role_is_read_only = await connection.scalar(
                            text(
                                "SELECT NOT r.rolsuper AND NOT r.rolcreatedb AND NOT r.rolcreaterole "
                                "AND NOT r.rolreplication AND NOT r.rolbypassrls "
                                "AND NOT has_database_privilege(current_user, current_database(), 'CREATE') "
                                "AND NOT EXISTS (SELECT 1 FROM pg_namespace ns "
                                "WHERE has_schema_privilege(current_user, ns.oid, 'CREATE')) "
                                "AND has_table_privilege(current_user, :relation, 'SELECT') "
                                "AND NOT EXISTS (SELECT 1 FROM pg_class writable "
                                "JOIN pg_namespace writable_ns ON writable_ns.oid = writable.relnamespace "
                                "WHERE writable.relkind IN ('r', 'p', 'v', 'm', 'f') "
                                "AND writable_ns.nspname NOT IN ('pg_catalog', 'information_schema') "
                                "AND writable_ns.nspname NOT LIKE 'pg_toast%' "
                                "AND writable_ns.nspname NOT LIKE 'pg_temp_%' "
                                "AND writable_ns.nspname NOT LIKE 'pg_toast_temp_%' AND ("
                                "has_table_privilege(current_user, writable.oid, 'INSERT') OR "
                                "has_table_privilege(current_user, writable.oid, 'UPDATE') OR "
                                "has_table_privilege(current_user, writable.oid, 'DELETE') OR "
                                "has_table_privilege(current_user, writable.oid, 'TRUNCATE') OR EXISTS ("
                                "SELECT 1 FROM pg_attribute a WHERE a.attrelid = writable.oid "
                                "AND a.attnum > 0 AND NOT a.attisdropped AND ("
                                "has_column_privilege(current_user, writable.oid, a.attnum, 'INSERT') OR "
                                "has_column_privilege(current_user, writable.oid, a.attnum, 'UPDATE'))))) "
                                "FROM pg_roles r WHERE r.rolname = current_user"
                            ),
                            {"relation": relation},
                        )
                        if role_is_read_only is not True:
                            raise DatabaseAnalysisUnavailable("The configured database account does not have read-only access.")
                        result = await connection.execute(statement, params)
                        fetched = result.fetchmany(limit + 1)
                        rows = [[_format_cell(value) for value in record] for record in fetched[:limit]]
                truncated = len(fetched) > limit
            finally:
                await engine.dispose()
    except (DatabaseAnalysisAccessDenied, DatabaseAnalysisConfigurationError, DatabaseAnalysisQueryError, DatabaseAnalysisUnavailable):
        raise
    except Exception:
        # Driver and SQL errors can contain connection details or source data; do not return or log them.
        raise DatabaseAnalysisUnavailable("The configured business database could not complete this read-only query.") from None

    retrieved_at = datetime.now(timezone.utc).isoformat()
    return DatabaseAnalysisResult(
        source_label=settings.rag_database_analysis_source_label,
        table=table.name,
        retrieved_at=retrieved_at,
        operation=query.operation,
        columns=result_columns,
        rows=rows,
        returned_rows=len(rows),
        row_limit=limit,
        truncated=truncated,
        provenance=f"Live query of {settings.rag_database_analysis_source_label} · {table.name} · {retrieved_at}",
    )
