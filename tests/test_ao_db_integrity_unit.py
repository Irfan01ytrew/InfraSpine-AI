"""Unit tests for Step AO slice 6: integrity verdict + orphan SQL shape (no database)."""

from app.ops.db_integrity import _orphan_count_sql, integrity_verdict


def test_verdict_passes_when_everything_is_validated_and_clean():
    verdict = integrity_verdict(
        [{"constraint_name": "fk_a", "validated": True}],
        [{"constraint_name": "fk_a", "orphans": 0}],
    )
    assert verdict == {
        "passed": True,
        "foreign_keys": 1,
        "unvalidated": [],
        "orphans": [],
        "total_orphans": 0,
    }


def test_verdict_fails_on_unvalidated_constraints_and_reports_orphans():
    verdict = integrity_verdict(
        [
            {"constraint_name": "fk_a", "validated": True},
            {"constraint_name": "fk_b", "validated": False},
        ],
        [
            {"constraint_name": "fk_a", "orphans": 3},
            {"constraint_name": "fk_b", "orphans": 0},
        ],
    )
    assert verdict["passed"] is False
    assert verdict["unvalidated"] == ["fk_b"]
    assert verdict["orphans"] == [{"constraint": "fk_a", "orphans": 3}]
    assert verdict["total_orphans"] == 3


def test_orphan_sql_excludes_null_foreign_keys_and_quotes_identifiers():
    sql = _orphan_count_sql(
        {
            "constraint_name": "fk_test",
            "child_table": 'weird "table',
            "parent_table": "parent",
            "child_columns": ["parent_id", "tenant_id"],
            "parent_columns": ["id", "tenant_id"],
        }
    )
    assert '"weird ""table"' in sql
    assert 'c."parent_id" IS NOT NULL' in sql
    assert 'c."tenant_id" IS NOT NULL' in sql
    assert 'c."parent_id" = p."id"' in sql
    assert 'c."tenant_id" = p."tenant_id"' in sql
    assert "NOT EXISTS" in sql
