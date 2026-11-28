"""Step AM slice 2 — tests for app/foundry/generate.py (data only).

No database, no network, no execution of generated code: the generator
returns files as data and the tests inspect that data. Run with
`python -m pytest tests/test_foundry_generate.py --noconftest`.
"""

import ast

import pytest

from app.foundry.generate import (
    compute_build_fingerprint,
    generate_app,
    scan_generated_content,
    slugify_name,
)
from app.foundry.spec import validate_spec


def _valid_spec(**overrides):
    spec = {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [
            {"name": "Requests", "path": "/requests",
             "endpoints": ["list_requests"]},
            {"name": "New request", "path": "/requests/new",
             "endpoints": ["create_request"]},
        ],
        "endpoints": [
            {"name": "list_requests", "method": "GET",
             "path": "/api/requests", "table": "leave_requests"},
            {"name": "create_request", "method": "POST",
             "path": "/api/requests", "table": "leave_requests"},
        ],
        "tables": [
            {"name": "leave_requests",
             "columns": [{"name": "id", "type": "uuid"},
                         {"name": "status", "type": "string"},
                         {"name": "days", "type": "integer"}]},
        ],
    }
    spec.update(overrides)
    return spec


def _generated(spec=None):
    spec = _valid_spec() if spec is None else spec
    return generate_app(spec, validate_spec(spec))


# --- generation only from a valid validation --------------------------------

def test_valid_spec_generates_app():
    app = _generated()
    assert app.app_name == "Leave Tracker"
    assert app.slug == "leave_tracker"
    assert app.fingerprint == validate_spec(_valid_spec()).build_plan.fingerprint


def test_generated_file_set_is_complete():
    app = _generated()
    kinds = {f.kind for f in app.files}
    assert kinds == {"interface", "backend", "migration", "schema", "test", "docs"}
    assert len(app.files_of_kind("interface")) == 2
    assert len(app.files_of_kind("backend")) == 1
    assert len(app.files_of_kind("migration")) == 1
    assert len(app.files_of_kind("schema")) == 1
    assert len(app.files_of_kind("test")) == 1
    assert len(app.files_of_kind("docs")) == 1


def test_invalid_validation_raises():
    spec = _valid_spec(purpose="x")
    with pytest.raises(ValueError, match="valid SpecValidation"):
        generate_app(spec, validate_spec(spec))


def test_non_validation_raises():
    with pytest.raises(ValueError):
        generate_app(_valid_spec(), None)


def test_fingerprint_mismatch_raises():
    spec = _valid_spec()
    validation = validate_spec(spec)
    changed = _valid_spec(purpose="Let employees request leave quickly.")
    with pytest.raises(ValueError, match="fingerprint"):
        generate_app(changed, validation)


def test_delete_endpoint_spec_cannot_generate():
    spec = _valid_spec(endpoints=[
        {"name": "delete_request", "method": "DELETE",
         "path": "/api/requests", "table": "leave_requests"}])
    with pytest.raises(ValueError):
        generate_app(spec, validate_spec(spec))


def test_payment_endpoint_spec_cannot_generate():
    spec = _valid_spec(endpoints=[
        {"name": "pay_invoice", "method": "POST",
         "path": "/api/pay", "table": "leave_requests"}])
    with pytest.raises(ValueError):
        generate_app(spec, validate_spec(spec))


def test_empty_spec_cannot_generate():
    spec = _valid_spec(pages=[], endpoints=[])
    with pytest.raises(ValueError):
        generate_app(spec, validate_spec(spec))


# --- determinism ------------------------------------------------------------

def test_generation_is_deterministic():
    first = _generated()
    second = _generated()
    assert first.build_fingerprint == second.build_fingerprint
    assert [f.path for f in first.files] == [f.path for f in second.files]
    assert [f.content for f in first.files] == [f.content for f in second.files]


def test_changed_spec_changes_build_fingerprint():
    first = _generated()
    changed = _generated(_valid_spec(
        purpose="Let employees request leave and managers approve it fast."))
    assert first.build_fingerprint != changed.build_fingerprint


def test_build_fingerprint_is_path_sorted():
    app = _generated()
    shuffled = tuple(reversed(app.files))
    assert compute_build_fingerprint(shuffled) == app.build_fingerprint


def test_slugify_name():
    assert slugify_name("Leave Tracker") == "leave_tracker"
    assert slugify_name("My  App--Name!") == "my_app_name"


# --- interface output --------------------------------------------------------

def test_interface_files_use_relative_paths_only():
    app = _generated()
    for generated in app.files_of_kind("interface"):
        assert "http://" not in generated.content
        assert "https://" not in generated.content
        assert "/api/requests" in generated.content


def test_interface_file_names_follow_page_paths():
    app = _generated()
    paths = {f.path for f in app.files_of_kind("interface")}
    assert "frontend/src/foundry/leave_tracker/requests.tsx" in paths
    assert "frontend/src/foundry/leave_tracker/requests_new.tsx" in paths


# --- backend output ---------------------------------------------------------

def test_backend_router_is_valid_python_with_both_handlers():
    app = _generated()
    backend = app.files_of_kind("backend")[0]
    tree = ast.parse(backend.content)
    names = {node.name for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "list_requests" in names
    assert "create_request" in names
    assert "APIRouter" in backend.content


def test_backend_contains_no_sql_or_execution():
    app = _generated()
    backend = app.files_of_kind("backend")[0]
    for token in ("SELECT", "INSERT INTO", "exec(", "eval(", "subprocess",
                  "os.system", "DATABASE_URL"):
        assert token not in backend.content


# --- migration + schema -----------------------------------------------------

def test_migration_draft_revision_matches_fingerprint():
    app = _generated()
    migration = app.files_of_kind("migration")[0]
    assert f'revision = "{app.fingerprint[:12]}"' in migration.content
    assert "DRAFT" in migration.content
    assert "never auto-applied" in migration.content.lower()
    assert 'op.create_table(\n        "leave_requests"' in migration.content
    assert 'op.drop_table("leave_requests")' in migration.content


def test_migration_column_types_mapped():
    app = _generated()
    migration = app.files_of_kind("migration")[0]
    assert 'sa.UUID(as_uuid=True)' in migration.content
    assert 'sa.String(255)' in migration.content
    assert 'sa.Integer' in migration.content


def test_schema_creates_declared_tables_only():
    app = _generated()
    schema = app.files_of_kind("schema")[0]
    assert 'CREATE TABLE IF NOT EXISTS "leave_requests"' in schema.content
    assert "production" not in schema.content.lower().replace(
        "never production data", "")


def test_two_table_spec_generates_both_tables():
    spec = _valid_spec(tables=[
        {"name": "leave_requests",
         "columns": [{"name": "id", "type": "uuid"}]},
        {"name": "approvals",
         "columns": [{"name": "id", "type": "uuid"},
                     {"name": "note", "type": "text"}]},
    ])
    app = _generated(spec)
    schema = app.files_of_kind("schema")[0]
    assert '"leave_requests"' in schema.content
    assert '"approvals"' in schema.content


# --- generated tests + docs ---------------------------------------------------

def test_generated_test_stubs_carry_required_ids():
    app = _generated()
    test_file = app.files_of_kind("test")[0]
    for test_id in ("endpoint:list_requests", "endpoint:create_request",
                    "table:leave_requests", "page:/requests",
                    "page:/requests/new",
                    "gate:approval_blocked_without_approval",
                    "gate:preview_disposable_db"):
        assert f"foundry-test-id: {test_id}" in test_file.content


def test_readme_contains_fingerprint_and_lists():
    app = _generated()
    readme = app.files_of_kind("docs")[0]
    assert app.fingerprint in readme.content
    assert "/requests" in readme.content
    assert "leave_requests" in readme.content


# --- safety re-scan ---------------------------------------------------------

def test_every_generated_file_scans_clean():
    app = _generated()
    for generated in app.files:
        assert scan_generated_content(generated.content) == ()


def test_scanner_flags_execution_and_network():
    assert scan_generated_content("x = exec(cmd)") != ()
    assert scan_generated_content("fetch('https://evil.example/x')") != ()
    assert scan_generated_content("SELECT * FROM users") != ()


def test_scanner_flags_secrets_and_destructive_text():
    assert scan_generated_content("password: hunter2") != ()
    assert scan_generated_content("DROP TABLE users") != ()


def test_file_lookup_helpers():
    app = _generated()
    schema = app.file("db/leave_tracker/schema.sql")
    assert schema is not None and schema.kind == "schema"
    assert app.file("db/leave_tracker/nope.sql") is None
