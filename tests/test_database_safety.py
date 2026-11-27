import pytest

from app.db.testing import validate_test_database_target


def test_dedicated_test_database_is_distinct_from_application_database():
    validate_test_database_target(
        "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai",
        "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai_test",
    )


def test_same_application_database_is_refused_even_when_name_ends_in_test():
    with pytest.raises(RuntimeError, match="configured application database"):
        validate_test_database_target(
            "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai_test",
            "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai_test",
        )


def test_same_database_name_is_refused_even_when_host_alias_differs():
    with pytest.raises(RuntimeError, match="configured application database"):
        validate_test_database_target(
            "postgresql+asyncpg://app:unused@localhost/infraspine_ai_test",
            "postgresql+asyncpg://app:unused@127.0.0.1:5432/infraspine_ai_test",
        )


def test_shared_database_requires_a_fresh_explicit_development_schema():
    app_url = "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai"
    with pytest.raises(RuntimeError):
        validate_test_database_target(app_url, app_url)

    validate_test_database_target(
        app_url,
        app_url,
        test_schema="infraspine_test_012345abcdef",
        shared_development_schema_allowed=True,
    )


def test_non_test_database_name_is_refused():
    with pytest.raises(RuntimeError, match="must end in _test"):
        validate_test_database_target(
            None,
            "postgresql+asyncpg://app:unused@db.example:5432/infraspine_ai",
        )
