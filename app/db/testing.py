"""Safety checks shared by the PostgreSQL integration-test harness."""

from sqlalchemy.engine import make_url

_TEST_SCHEMA_PREFIX = "infraspine_test_"


TEST_SCHEMA_RESET_SQL = """
DO $test_reset$
DECLARE
    table_list text;
BEGIN
    SELECT string_agg(format('%I.%I', n.nspname, c.relname), ', ' ORDER BY c.relname)
      INTO table_list
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema()
       AND c.relkind IN ('r', 'p')
       AND c.relname NOT IN ('alembic_version', 'rag_terms')
       AND NOT EXISTS (
            SELECT 1 FROM pg_inherits AS i WHERE i.inhrelid = c.oid
       );

    IF table_list IS NOT NULL THEN
        -- All tables in the isolated schema are named explicitly, so dependent-table expansion is unnecessary.
        -- Dynamic SQL keeps this reset out of CacheMemoryContext's long-lived cached-plan list.
        EXECUTE 'TRUNCATE TABLE ' || table_list || ' RESTART IDENTITY';
    END IF;
END
$test_reset$;
"""


def validate_test_database_target(
    application_url: str | None,
    test_url: str,
    *,
    test_schema: str | None = None,
    shared_development_schema_allowed: bool = False,
) -> None:
    """Refuse destructive test migrations against the configured application database."""
    app = make_url(application_url) if application_url else None
    test = make_url(test_url)
    same_database_name = bool(app and test.database == app.database)
    same_endpoint = bool(
        app
        and (test.host or "").casefold() == (app.host or "").casefold()
        and (test.port or 5432) == (app.port or 5432)
        and test.database == app.database
    )
    # Reject the same database name even when a URL uses a host alias (for example localhost vs
    # 127.0.0.1). A false positive for equal names on separate clusters is safer than truncating
    # the application database through an alias the test harness cannot resolve locally.
    same_database = same_database_name or same_endpoint
    schema_is_valid = bool(
        test_schema is None
        or (
            len(test_schema) == len(f"{_TEST_SCHEMA_PREFIX}{'0' * 12}")
            and test_schema.startswith(_TEST_SCHEMA_PREFIX)
            and all(character in "0123456789abcdef" for character in test_schema.removeprefix(_TEST_SCHEMA_PREFIX))
        )
    )
    if not schema_is_valid:
        raise RuntimeError("The test schema name must use infraspine_test_<12 lowercase hex characters>.")
    safe_shared_schema = bool(
        shared_development_schema_allowed
        and same_database
        and test_schema
    )
    if same_database and not safe_shared_schema:
        raise RuntimeError("Refusing to run tests against the configured application database.")
    if not (test.database or "").endswith("_test") and not safe_shared_schema:
        raise RuntimeError("The PostgreSQL test database name must end in _test.")
