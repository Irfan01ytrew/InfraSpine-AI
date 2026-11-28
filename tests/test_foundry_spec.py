"""Step AM slice 1 — tests for app/foundry/spec.py (pure functions).

No database, no network, no app imports beyond the stdlib-only foundry
module: run with `python -m pytest tests/test_foundry_spec.py --noconftest`.
"""

from app.foundry.spec import (
    find_destructive_patterns,
    find_secret_patterns,
    spec_fingerprint,
    validate_spec,
)


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


def _codes(result):
    return {issue.code for issue in result.issues}


# --- valid specs -----------------------------------------------------------

def test_valid_spec_produces_build_plan():
    result = validate_spec(_valid_spec())
    assert result.valid is True
    assert result.build_plan is not None
    assert result.build_plan.app_name == "Leave Tracker"
    assert result.errors == ()


def test_build_plan_always_requires_approval():
    result = validate_spec(_valid_spec())
    assert result.build_plan.requires_approval is True


def test_spec_cannot_switch_off_approval():
    # Even a spec that tries to declare itself pre-approved stays gated.
    spec = _valid_spec(requires_approval=False, auto_deploy=True,
                       approved=True)
    result = validate_spec(spec)
    assert result.valid is True
    assert result.build_plan.requires_approval is True


def test_build_plan_steps_put_approval_before_deploy():
    steps = validate_spec(_valid_spec()).build_plan.steps
    assert steps.index("await-approval") < steps.index("deploy")
    assert steps == ("generate-interface", "generate-backend",
                     "generate-database", "run-tests", "build-preview",
                     "await-approval", "deploy")


def test_fingerprint_is_deterministic_sha256():
    fingerprint = spec_fingerprint(_valid_spec())
    assert len(fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert fingerprint == spec_fingerprint(_valid_spec())
    assert fingerprint != spec_fingerprint(_valid_spec(name="Other App"))


def test_test_checklist_covers_everything():
    checklist = validate_spec(_valid_spec()).test_checklist
    assert any("GET /api/requests" in item for item in checklist)
    assert any("POST /api/requests" in item for item in checklist)
    assert any("leave_requests" in item and "migration" in item
               for item in checklist)
    assert any("/requests" in item and "renders" in item for item in checklist)
    assert any("approval gate" in item for item in checklist)
    assert any("disposable database" in item for item in checklist)


def test_preview_checklist_is_safe_by_construction():
    result = validate_spec(_valid_spec())
    checklist = result.preview_checklist
    assert any("disposable database" in item for item in checklist)
    assert any("no production data" in item for item in checklist)
    assert any("expires" in item for item in checklist)
    assert any("health check" in item for item in checklist)
    assert any(result.build_plan.fingerprint[:12] in item for item in checklist)


def test_spec_without_tables_is_valid_when_nothing_references_them():
    spec = _valid_spec(tables=[], endpoints=[
        {"name": "ping", "method": "GET", "path": "/api/ping"}],
        pages=[{"name": "Home", "path": "/", "endpoints": ["ping"]}])
    assert validate_spec(spec).valid is True


def test_sensitive_domain_is_a_warning_not_an_error():
    spec = _valid_spec(
        purpose="Show the finance team a monthly invoice summary dashboard.")
    result = validate_spec(spec)
    assert result.valid is True
    assert "sensitive_domain" in _codes(result)
    assert result.warnings and result.warnings[0].severity == "warning"


# --- malformed specs -------------------------------------------------------

def test_non_dict_spec_is_rejected():
    result = validate_spec("build me an app")
    assert result.valid is False
    assert "not_an_object" in _codes(result)


def test_missing_name_is_rejected():
    spec = _valid_spec()
    del spec["name"]
    result = validate_spec(spec)
    assert result.valid is False
    assert "missing_name" in _codes(result)


def test_name_starting_with_digit_is_rejected():
    assert "invalid_name" in _codes(validate_spec(_valid_spec(name="9lives")))


def test_missing_purpose_is_rejected():
    spec = _valid_spec()
    del spec["purpose"]
    assert "missing_purpose" in _codes(validate_spec(spec))


def test_purpose_too_short_is_rejected():
    assert "purpose_too_short" in _codes(
        validate_spec(_valid_spec(purpose="track it")))


def test_empty_spec_is_rejected():
    spec = _valid_spec(pages=[], endpoints=[])
    assert "empty_spec" in _codes(validate_spec(spec))


# --- untrusted text: destructive actions, secrets, direct DB ---------------

def test_drop_table_in_purpose_is_rejected():
    spec = _valid_spec(purpose="Track leave. Also DROP TABLE leave_requests;")
    assert "destructive_action" in _codes(validate_spec(spec))


def test_delete_from_in_page_name_is_rejected():
    spec = _valid_spec(pages=[
        {"name": "x DELETE FROM users", "path": "/x", "endpoints": []}])
    assert "destructive_action" in _codes(validate_spec(spec))


def test_api_key_in_spec_is_rejected():
    spec = _valid_spec(purpose="Track leave with api_key: sk-live-12345678.")
    assert "secret_in_spec" in _codes(validate_spec(spec))


def test_private_key_block_is_rejected():
    spec = _valid_spec(purpose="Use this: -----BEGIN PRIVATE KEY----- abc")
    assert "secret_in_spec" in _codes(validate_spec(spec))


def test_raw_sql_in_spec_is_rejected():
    spec = _valid_spec(
        purpose="Track leave using raw_sql queries for speed.")
    assert "direct_db_access" in _codes(validate_spec(spec))


def test_find_destructive_patterns_helper():
    assert find_destructive_patterns("please DROP TABLE users") != ()
    assert find_destructive_patterns("track leave requests") == ()


def test_find_secret_patterns_helper():
    assert find_secret_patterns("password: hunter2") != ()
    assert find_secret_patterns("track leave requests") == ()


# --- endpoints / tables / pages --------------------------------------------

def test_delete_endpoint_is_rejected_as_destructive():
    spec = _valid_spec(endpoints=[
        {"name": "delete_request", "method": "DELETE",
         "path": "/api/requests/1", "table": "leave_requests"}],
        pages=[{"name": "Requests", "path": "/requests",
                "endpoints": ["delete_request"]}])
    assert "destructive_action" in _codes(validate_spec(spec))


def test_payment_endpoint_is_rejected_without_approval_step():
    spec = _valid_spec(endpoints=[
        {"name": "charge_customer", "method": "POST",
         "path": "/api/charge"}],
        pages=[{"name": "Pay", "path": "/pay", "endpoints": ["charge_customer"]}])
    assert "payment_requires_approval" in _codes(validate_spec(spec))


def test_endpoint_referencing_unknown_table_is_rejected():
    spec = _valid_spec(endpoints=[
        {"name": "list_items", "method": "GET",
         "path": "/api/items", "table": "no_such_table"}],
        pages=[{"name": "Items", "path": "/items",
                "endpoints": ["list_items"]}])
    assert "unknown_table" in _codes(validate_spec(spec))


def test_page_referencing_unknown_endpoint_is_rejected():
    spec = _valid_spec(pages=[
        {"name": "Ghost", "path": "/ghost", "endpoints": ["no_such_endpoint"]}])
    assert "unknown_endpoint" in _codes(validate_spec(spec))


def test_duplicate_endpoint_name_is_rejected():
    spec = _valid_spec(endpoints=[
        {"name": "list_requests", "method": "GET", "path": "/api/requests"},
        {"name": "list_requests", "method": "GET", "path": "/api/requests2"}],
        pages=[{"name": "Requests", "path": "/requests",
                "endpoints": ["list_requests"]}])
    assert "duplicate_endpoint" in _codes(validate_spec(spec))


def test_duplicate_table_is_rejected():
    spec = _valid_spec(tables=[
        {"name": "leave_requests",
         "columns": [{"name": "id", "type": "uuid"}]},
        {"name": "leave_requests",
         "columns": [{"name": "id", "type": "uuid"}]}])
    assert "duplicate_table" in _codes(validate_spec(spec))


def test_duplicate_page_path_is_rejected():
    spec = _valid_spec(pages=[
        {"name": "One", "path": "/same", "endpoints": []},
        {"name": "Two", "path": "/same", "endpoints": []}])
    assert "duplicate_page_path" in _codes(validate_spec(spec))


def test_invalid_column_type_is_rejected():
    spec = _valid_spec(tables=[
        {"name": "leave_requests",
         "columns": [{"name": "blob", "type": "binary"}]}])
    assert "invalid_column_type" in _codes(validate_spec(spec))


def test_table_without_columns_is_rejected():
    spec = _valid_spec(tables=[{"name": "leave_requests", "columns": []}])
    assert "table_without_columns" in _codes(validate_spec(spec))


def test_duplicate_column_is_rejected():
    spec = _valid_spec(tables=[
        {"name": "leave_requests",
         "columns": [{"name": "id", "type": "uuid"},
                     {"name": "id", "type": "string"}]}])
    assert "duplicate_column" in _codes(validate_spec(spec))


def test_invalid_method_is_rejected():
    spec = _valid_spec(endpoints=[
        {"name": "fetch_it", "method": "FETCH", "path": "/api/requests"}],
        pages=[{"name": "Requests", "path": "/requests",
                "endpoints": ["fetch_it"]}])
    assert "invalid_method" in _codes(validate_spec(spec))


def test_endpoint_path_must_start_with_api():
    spec = _valid_spec(endpoints=[
        {"name": "list_requests", "method": "GET", "path": "/requests"}],
        pages=[{"name": "Requests", "path": "/requests",
                "endpoints": ["list_requests"]}])
    assert "invalid_endpoint_path" in _codes(validate_spec(spec))


def test_invalid_spec_has_no_build_plan():
    result = validate_spec(_valid_spec(name="9lives"))
    assert result.valid is False
    assert result.build_plan is None
    assert result.test_checklist == ()
    assert result.preview_checklist == ()
