from app.db.testing import TEST_SCHEMA_RESET_SQL


def test_test_schema_reset_uses_current_schema_and_excludes_alembic_state():
    assert "n.nspname = current_schema()" in TEST_SCHEMA_RESET_SQL
    # Migration state and the seeded lexical term table survive a per-test reset.
    assert "c.relname NOT IN ('alembic_version', 'rag_terms')" in TEST_SCHEMA_RESET_SQL
    assert "c.relkind IN ('r', 'p')" in TEST_SCHEMA_RESET_SQL


def test_test_schema_reset_is_single_dynamic_non_cascading_truncate():
    assert "EXECUTE 'TRUNCATE TABLE ' || table_list || ' RESTART IDENTITY'" in TEST_SCHEMA_RESET_SQL
    assert "CASCADE" not in TEST_SCHEMA_RESET_SQL
    assert "FOR r IN" not in TEST_SCHEMA_RESET_SQL


def test_test_schema_reset_does_not_include_partition_children_twice():
    assert "FROM pg_inherits AS i WHERE i.inhrelid = c.oid" in TEST_SCHEMA_RESET_SQL
