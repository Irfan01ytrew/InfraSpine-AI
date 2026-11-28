"""Step AM (Self-Evolving Software Foundry), slice 1 — app spec validation.

Pure functions only: no code execution, no database access, no network calls,
no imports from the rest of the app (stdlib only, so this module is safe to run
anywhere, including before the platform is initialised).

A *spec* is the plain-dict description of one generated application:

    {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [{"name": "Requests", "path": "/requests",
                   "endpoints": ["list_requests"]}],
        "endpoints": [{"name": "list_requests", "method": "GET",
                       "path": "/api/requests", "table": "leave_requests"}],
        "tables": [{"name": "leave_requests",
                    "columns": [{"name": "id", "type": "uuid"},
                                {"name": "status", "type": "string"}]}],
    }

`validate_spec()` turns such a dict into a `SpecValidation`: either valid, with
a deterministic `BuildPlan` (including a SHA-256 fingerprint the deploy
approval is bound to), a test checklist and a preview checklist — or invalid,
with a named issue for every problem.

Safety, enforced structurally (never by convention):
* `BuildPlan.requires_approval` is always True — no input can turn it off.
  Deploy happens only after the user approves the exact build fingerprint.
* Spec text is untrusted: destructive statements, secrets, direct database /
  payment access are rejected, never obeyed. A spec can never grant a
  permission, change a payee, or widen what an approval covers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

__all__ = [
    "BuildPlan",
    "SpecIssue",
    "SpecValidation",
    "find_destructive_patterns",
    "find_secret_patterns",
    "spec_fingerprint",
    "validate_spec",
]

# ---------------------------------------------------------------------------
# Limits and vocabularies
# ---------------------------------------------------------------------------

MAX_NAME_LEN = 64
MAX_PURPOSE_LEN = 500
MAX_PAGES = 20
MAX_ENDPOINTS = 50
MAX_TABLES = 20
MAX_COLUMNS_PER_TABLE = 30

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
COLUMN_TYPES = ("string", "text", "integer", "number", "boolean", "datetime", "uuid")

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{2,63}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_API_PATH_RE = re.compile(r"^/api/[A-Za-z0-9/_-]+$")
_PAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")

# Destructive statements / actions: never allowed inside a spec. A generated
# app that needs one must go through an approval-gated workflow step instead
# (slice 4); the spec validator cannot grant it.
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\b.*\bDROP\b", re.IGNORECASE),
    re.compile(r"\bGRANT\b|\bREVOKE\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bSHUTDOWN\b|\bFORMAT\b", re.IGNORECASE),
    re.compile(r"\bexec\s*\(", re.IGNORECASE),
    re.compile(r"\beval\s*\(", re.IGNORECASE),
)

# Things that look like secrets. A spec containing one is rejected outright;
# generated apps receive configuration through the platform secret store.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|api[_-]?secret|password|passwd|secret[_-]?key|"
               r"access[_-]?token|auth[_-]?token|private[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bpassword\s*[:=]"),
)

# Direct database / raw-query access: the generator may only touch the
# database through the platform and the declared tables.
_DIRECT_DB_PATTERNS = (
    re.compile(r"(?i)\braw[_\s-]?sql\b"),
    re.compile(r"(?i)\bsql\s+query\b"),
    re.compile(r"(?i)\bdirect\s+db\b"),
    re.compile(r"(?i)\bexecute\s+sql\b"),
)

# Money movement: never generated without an approval step. Slice 1 has no
# approval-step field, so any payment endpoint is rejected here; slice 4 adds
# the approval-gated path through app/finance.
_PAYMENT_WORDS = ("pay", "payment", "charge", "transfer", "refund", "payout", "withdraw")

_SENSITIVE_DOMAIN_WORDS = ("payment", "payroll", "salary", "bank", "finance", "invoice")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecIssue:
    """One problem (or warning) found in a spec."""

    code: str
    field: str
    message: str
    severity: str = "error"  # "error" blocks the build; "warning" does not


@dataclass(frozen=True)
class BuildPlan:
    """Deterministic build plan for a validated spec.

    `requires_approval` is always True: deploy is gated on the user approving
    the exact `fingerprint` of this spec. No caller can set it to False.
    """

    app_name: str
    fingerprint: str
    requires_approval: bool = True
    steps: tuple[str, ...] = (
        "generate-interface",
        "generate-backend",
        "generate-database",
        "run-tests",
        "build-preview",
        "await-approval",
        "deploy",
    )


@dataclass(frozen=True)
class SpecValidation:
    """Outcome of `validate_spec()`."""

    valid: bool
    issues: tuple[SpecIssue, ...] = ()
    build_plan: BuildPlan | None = None
    test_checklist: tuple[str, ...] = ()
    preview_checklist: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[SpecIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[SpecIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")


# ---------------------------------------------------------------------------
# Text scanning helpers (also useful to slice 2, which must re-check
# generator output before it is executed anywhere)
# ---------------------------------------------------------------------------

def find_destructive_patterns(text: str) -> tuple[str, ...]:
    """Return the destructive patterns matched in `text` (empty when clean)."""
    if not isinstance(text, str):
        return ()
    return tuple(p.pattern for p in _DESTRUCTIVE_PATTERNS if p.search(text))


def find_secret_patterns(text: str) -> tuple[str, ...]:
    """Return the secret-like patterns matched in `text` (empty when clean)."""
    if not isinstance(text, str):
        return ()
    return tuple(p.pattern for p in _SECRET_PATTERNS if p.search(text))


def _iter_strings(value, path: str = ""):
    """Yield (path, string) for every string nested inside `value`."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{index}]")


def _scan_untrusted_text(spec: dict, issues: list[SpecIssue]) -> None:
    """Reject destructive statements, secrets and direct-DB access anywhere
    in the spec. Spec text is untrusted data: it is scanned, never obeyed."""
    for path, text in _iter_strings(spec):
        for pattern in find_destructive_patterns(text):
            issues.append(SpecIssue(
                code="destructive_action", field=path,
                message=f"Destructive action is not allowed in a spec "
                        f"(matched {pattern!r}); use an approval-gated "
                        f"workflow step instead."))
        for pattern in find_secret_patterns(text):
            issues.append(SpecIssue(
                code="secret_in_spec", field=path,
                message="Secrets are never allowed in a spec; generated apps "
                        "receive configuration through the platform secret store."))
        for pattern in _DIRECT_DB_PATTERNS:
            if pattern.search(text):
                issues.append(SpecIssue(
                    code="direct_db_access", field=path,
                    message="Direct database access is not allowed; the "
                            "generated app may only use its declared tables "
                            "through the platform."))


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def spec_fingerprint(spec: dict) -> str:
    """SHA-256 of the canonical JSON of the spec.

    The deploy approval (slice 4) is bound to this exact value: any change to
    the spec changes the fingerprint and invalidates the approval.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_list(value) -> bool:
    return isinstance(value, list)


def validate_spec(spec) -> SpecValidation:
    """Validate an app spec dict into a build plan + checklists, or issues.

    Never raises on malformed input: every problem is reported as an issue.
    """
    issues: list[SpecIssue] = []

    if not isinstance(spec, dict):
        return SpecValidation(valid=False, issues=(SpecIssue(
            code="not_an_object", field="spec",
            message="An app spec must be an object with name, purpose, pages, "
                    "endpoints and tables."),))

    _scan_untrusted_text(spec, issues)

    # -- name ---------------------------------------------------------------
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(SpecIssue(code="missing_name", field="name",
                                message="The app needs a name."))
    elif len(name) > MAX_NAME_LEN or not _NAME_RE.match(name):
        issues.append(SpecIssue(
            code="invalid_name", field="name",
            message="Name must start with a letter, be 3–64 characters, and "
                    "contain only letters, digits, spaces, '-' or '_'."))

    # -- purpose ------------------------------------------------------------
    purpose = spec.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        issues.append(SpecIssue(code="missing_purpose", field="purpose",
                                message="The app needs a purpose describing "
                                        "what it is for."))
    elif len(purpose) > MAX_PURPOSE_LEN:
        issues.append(SpecIssue(code="purpose_too_long", field="purpose",
                                message=f"Purpose must be at most "
                                        f"{MAX_PURPOSE_LEN} characters."))
    elif len(purpose.strip()) < 10:
        issues.append(SpecIssue(code="purpose_too_short", field="purpose",
                                message="Purpose must be at least 10 characters "
                                        "so the generator knows what to build."))
    if isinstance(purpose, str) and any(
            w in purpose.lower() for w in _SENSITIVE_DOMAIN_WORDS):
        issues.append(SpecIssue(
            code="sensitive_domain", field="purpose",
            message="This app touches a sensitive domain (money/finance); "
                    "deploy needs a Finance approval in addition to the "
                    "owner's approval.", severity="warning"))

    # -- tables -------------------------------------------------------------
    tables = spec.get("tables", [])
    table_names: set[str] = set()
    if not _is_list(tables):
        issues.append(SpecIssue(code="tables_not_a_list", field="tables",
                                message="Tables must be a list."))
        tables = []
    if len(tables) > MAX_TABLES:
        issues.append(SpecIssue(code="too_many_tables", field="tables",
                                message=f"At most {MAX_TABLES} tables per app."))
    for index, table in enumerate(tables):
        field_path = f"tables[{index}]"
        if not isinstance(table, dict):
            issues.append(SpecIssue(code="table_not_an_object", field=field_path,
                                    message="Each table must be an object."))
            continue
        tname = table.get("name")
        if not isinstance(tname, str) or not _TABLE_RE.match(tname or ""):
            issues.append(SpecIssue(
                code="invalid_table_name", field=f"{field_path}.name",
                message="Table names must be snake_case, starting with a "
                        "letter (e.g. leave_requests)."))
            continue
        if tname in table_names:
            issues.append(SpecIssue(code="duplicate_table", field=f"{field_path}.name",
                                    message=f"Duplicate table name {tname!r}."))
        table_names.add(tname)
        columns = table.get("columns")
        if not _is_list(columns) or not columns:
            issues.append(SpecIssue(
                code="table_without_columns", field=f"{field_path}.columns",
                message=f"Table {tname!r} needs at least one column."))
            continue
        if len(columns) > MAX_COLUMNS_PER_TABLE:
            issues.append(SpecIssue(
                code="too_many_columns", field=f"{field_path}.columns",
                message=f"Table {tname!r} has more than "
                        f"{MAX_COLUMNS_PER_TABLE} columns."))
        seen_columns: set[str] = set()
        for c_index, column in enumerate(columns):
            c_path = f"{field_path}.columns[{c_index}]"
            if not isinstance(column, dict):
                issues.append(SpecIssue(code="column_not_an_object", field=c_path,
                                        message="Each column must be an object."))
                continue
            cname = column.get("name")
            if not isinstance(cname, str) or not _COLUMN_RE.match(cname or ""):
                issues.append(SpecIssue(
                    code="invalid_column_name", field=f"{c_path}.name",
                    message="Column names must be snake_case, starting with "
                            "a letter."))
                continue
            if cname in seen_columns:
                issues.append(SpecIssue(
                    code="duplicate_column", field=f"{c_path}.name",
                    message=f"Duplicate column {cname!r} in table {tname!r}."))
            seen_columns.add(cname)
            ctype = column.get("type")
            if ctype not in COLUMN_TYPES:
                issues.append(SpecIssue(
                    code="invalid_column_type", field=f"{c_path}.type",
                    message=f"Column {cname!r} has unsupported type "
                            f"{ctype!r}; allowed: {', '.join(COLUMN_TYPES)}."))

    # -- endpoints ----------------------------------------------------------
    endpoints = spec.get("endpoints", [])
    endpoint_names: set[str] = set()
    if not _is_list(endpoints):
        issues.append(SpecIssue(code="endpoints_not_a_list", field="endpoints",
                                message="Endpoints must be a list."))
        endpoints = []
    if len(endpoints) > MAX_ENDPOINTS:
        issues.append(SpecIssue(code="too_many_endpoints", field="endpoints",
                                message=f"At most {MAX_ENDPOINTS} endpoints per app."))
    for index, endpoint in enumerate(endpoints):
        field_path = f"endpoints[{index}]"
        if not isinstance(endpoint, dict):
            issues.append(SpecIssue(code="endpoint_not_an_object", field=field_path,
                                    message="Each endpoint must be an object."))
            continue
        ename = endpoint.get("name")
        if not isinstance(ename, str) or not _SLUG_RE.match(ename or ""):
            issues.append(SpecIssue(
                code="invalid_endpoint_name", field=f"{field_path}.name",
                message="Endpoint names must be snake_case, starting with a "
                        "letter (e.g. list_requests)."))
            continue
        if ename in endpoint_names:
            issues.append(SpecIssue(
                code="duplicate_endpoint", field=f"{field_path}.name",
                message=f"Duplicate endpoint name {ename!r}."))
        endpoint_names.add(ename)
        method = endpoint.get("method")
        if method not in HTTP_METHODS:
            issues.append(SpecIssue(
                code="invalid_method", field=f"{field_path}.method",
                message=f"Method must be one of {', '.join(HTTP_METHODS)}."))
        elif method == "DELETE":
            issues.append(SpecIssue(
                code="destructive_action", field=f"{field_path}.method",
                message="DELETE endpoints are destructive and are not allowed "
                        "in a generated app; use an approval-gated workflow "
                        "step instead."))
        path = endpoint.get("path")
        if not isinstance(path, str) or not _API_PATH_RE.match(path or ""):
            issues.append(SpecIssue(
                code="invalid_endpoint_path", field=f"{field_path}.path",
                message="Endpoint paths must start with /api/ and contain "
                        "only letters, digits, '/', '_' or '-'."))
        table = endpoint.get("table")
        if table is not None:
            if not isinstance(table, str) or table not in table_names:
                issues.append(SpecIssue(
                    code="unknown_table", field=f"{field_path}.table",
                    message=f"Endpoint {ename!r} references unknown table "
                            f"{table!r}; declare it in tables first."))
        lowered = f"{ename or ''} {path or ''}".lower()
        if any(word in lowered for word in _PAYMENT_WORDS):
            issues.append(SpecIssue(
                code="payment_requires_approval", field=field_path,
                message=f"Endpoint {ename!r} moves money; generated apps "
                        "never move money without an approval step — add an "
                        "approval-gated Finance workflow (slice 4) instead."))

    # -- pages --------------------------------------------------------------
    pages = spec.get("pages", [])
    page_paths: set[str] = set()
    if not _is_list(pages):
        issues.append(SpecIssue(code="pages_not_a_list", field="pages",
                                message="Pages must be a list."))
        pages = []
    if len(pages) > MAX_PAGES:
        issues.append(SpecIssue(code="too_many_pages", field="pages",
                                message=f"At most {MAX_PAGES} pages per app."))
    for index, page in enumerate(pages):
        field_path = f"pages[{index}]"
        if not isinstance(page, dict):
            issues.append(SpecIssue(code="page_not_an_object", field=field_path,
                                    message="Each page must be an object."))
            continue
        pname = page.get("name")
        if not isinstance(pname, str) or not pname.strip():
            issues.append(SpecIssue(code="missing_page_name", field=f"{field_path}.name",
                                    message="Each page needs a name."))
        path = page.get("path")
        if not isinstance(path, str) or not _PAGE_PATH_RE.match(path or ""):
            issues.append(SpecIssue(
                code="invalid_page_path", field=f"{field_path}.path",
                message="Page paths must start with '/' and contain only "
                        "letters, digits, '/', '_' or '-'."))
        elif path in page_paths:
            issues.append(SpecIssue(code="duplicate_page_path",
                                    field=f"{field_path}.path",
                                    message=f"Duplicate page path {path!r}."))
        if isinstance(path, str):
            page_paths.add(path)
        for ref in page.get("endpoints", []) or []:
            if ref not in endpoint_names:
                issues.append(SpecIssue(
                    code="unknown_endpoint", field=f"{field_path}.endpoints",
                    message=f"Page {pname!r} references unknown endpoint "
                            f"{ref!r}; declare it in endpoints first."))

    # -- empty app ----------------------------------------------------------
    if _is_list(pages) and _is_list(endpoints) and not pages and not endpoints:
        issues.append(SpecIssue(code="empty_spec", field="spec",
                                message="The spec has no pages and no "
                                        "endpoints — there is nothing to build."))

    errors = [i for i in issues if i.severity == "error"]
    if errors:
        return SpecValidation(valid=False, issues=tuple(issues))

    # -- valid: deterministic build plan + checklists -----------------------
    assert isinstance(name, str) and isinstance(purpose, str)
    plan = BuildPlan(app_name=name.strip(), fingerprint=spec_fingerprint(spec))

    test_checklist = [
        "test approval gate blocks deploy without approval",
        "test preview uses a disposable database with seed data only",
    ]
    for endpoint in endpoints:
        test_checklist.append(
            f"test endpoint {endpoint.get('method')} {endpoint.get('path')} "
            f"({endpoint.get('name')})")
    for table in tables:
        test_checklist.append(
            f"test table {table.get('name')} migration applies and rolls back")
    for page in pages:
        test_checklist.append(f"test page {page.get('path')} renders")

    preview_checklist = (
        "preview uses a disposable database (no production data)",
        "preview link is temporary and expires",
        f"preview shows build fingerprint {plan.fingerprint[:12]}…",
        "preview lists every page, endpoint and table in the spec",
        "preview health check passes before approval is offered",
    )

    return SpecValidation(
        valid=True,
        issues=tuple(issues),
        build_plan=plan,
        test_checklist=tuple(test_checklist),
        preview_checklist=preview_checklist,
    )
