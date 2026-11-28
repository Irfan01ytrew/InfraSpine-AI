"""Unit tests for Step AO slice 4: role SQL, quoting, password rules (no database)."""

import pytest

from app.ops.db_roles import (
    ROLE_APP,
    ROLE_BACKUP,
    ROLE_MIGRATOR,
    ROLE_REPORTING,
    quote_identifier,
    quote_literal,
    role_statements,
    validate_role_name,
    validate_role_password,
)


def test_role_statements_give_the_app_read_write_but_no_ddl():
    statements = role_statements(ROLE_APP)
    joined = "\n".join(statements)
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" in joined
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" in joined
    assert "REVOKE CREATE ON SCHEMA" in joined
    assert "GRANT CREATE ON SCHEMA" not in joined
    assert "NOSUPERUSER" in joined


def test_role_statements_give_reporting_select_only():
    statements = role_statements(ROLE_REPORTING)
    joined = "\n".join(statements)
    assert "GRANT SELECT ON ALL TABLES" in joined
    assert "INSERT" not in joined
    assert "ON ALL SEQUENCES" not in joined
    assert "REVOKE CREATE ON SCHEMA" in joined


def test_role_statements_give_the_backup_role_read_only():
    statements = role_statements(ROLE_BACKUP)
    joined = "\n".join(statements)
    assert "GRANT SELECT ON ALL TABLES" in joined
    assert "INSERT" not in joined
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" in joined


def test_role_statements_give_the_migrator_ddl_and_nothing_else_extra():
    statements = role_statements(ROLE_MIGRATOR)
    joined = "\n".join(statements)
    assert "GRANT CREATE ON SCHEMA" in joined
    assert "REVOKE CREATE" not in joined


def test_role_statements_reject_unknown_roles_and_schemas():
    with pytest.raises(ValueError, match="unknown AO role"):
        role_statements("postgres")
    with pytest.raises(ValueError, match="unknown AO role"):
        role_statements("infraspine_app; DROP TABLE users")
    with pytest.raises(ValueError):
        role_statements(ROLE_APP, schema="public; DROP TABLE users")


def test_quoting_neutralizes_injection_strings():
    # A quote inside a name is doubled, never terminates the identifier.
    assert quote_identifier('evil"; DROP TABLE users; --') == '"evil""; DROP TABLE users; --"'
    assert quote_literal("pa'ss'word") == "'pa''ss''word'"
    assert quote_identifier("infraspine_app") == '"infraspine_app"'


def test_validate_role_name_accepts_only_the_five_roles():
    assert validate_role_name(ROLE_APP) == ROLE_APP
    with pytest.raises(ValueError):
        validate_role_name("postgres")


def test_role_password_rules():
    assert validate_role_password("correct horse battery staple 7") == []
    problems = validate_role_password("short1")
    assert any("at least 16" in problem for problem in problems)
    problems = validate_role_password("a" * 20)
    assert any("digit" in problem for problem in problems)
    problems = validate_role_password("1" * 20)
    assert any("letter" in problem for problem in problems)
