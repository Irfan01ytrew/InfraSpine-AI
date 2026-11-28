"""Step AM (Self-Evolving Software Foundry), slice 3 — test + preview.

Takes a generated app (slice 2, data only) and proves it in the QA sandbox
before any approval is offered:

* `build_preview()` enforces slice 1's preview checklist against the
  generated files as data: disposable database only, expiring preview link,
  fingerprint shown, every spec item listed, health check recorded.
* `evaluate_test_results()` maps test results (by foundry-test-id) onto the
  required test ids from the spec. Any failure — or any missing test —
  blocks the gate. There is no partial pass.
* `decide_gate()` combines preview checks + test results + sandbox health:
  approval is offered only when everything passes. `approval_offered()` is
  True only for a `preview_ready` preview.
* `verify_in_sandbox()` runs the generated `schema.sql` in a *disposable*
  PostgreSQL cluster (`app/qa/sandbox.py: disposable_postgres`) — seed data
  only — and verifies every declared table exists, accepts one seed row,
  and reports a row count. It never touches any other database: the
  connection URL must be a 127.0.0.1 disposable URL, and the sandbox's own
  clean environment replaces `DATABASE_URL` with an invalid placeholder.

Pure functions throughout, except `verify_in_sandbox()`, which is the one
place generated database code is executed — inside the disposable sandbox
only, never against production, never with real data.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from app.foundry.generate import (
    GeneratedApp,
    compute_build_fingerprint,
    scan_generated_content,
)
from app.foundry.spec import SpecValidation, spec_fingerprint

__all__ = [
    "GateDecision",
    "Preview",
    "PreviewCheck",
    "SandboxVerification",
    "TestResult",
    "apply_gate",
    "approval_offered",
    "build_preview",
    "check_generated_consistency",
    "decide_gate",
    "evaluate_test_results",
    "required_test_ids",
    "verify_in_sandbox",
]

MAX_PREVIEW_TTL_SECONDS = 3600  # previews expire after at most one hour

_CREATE_TABLE_RE = re.compile(r'CREATE TABLE IF NOT EXISTS\s+"([a-z][a-z0-9_]*)"')
_MIGRATION_TABLE_RE = re.compile(r'op\.create_table\(\s*"([a-z][a-z0-9_]*)"')

_SEED_LITERAL_BY_TYPE = {
    "string": "'seed'",
    "text": "'seed'",
    "integer": "1",
    "number": "1.5",
    "boolean": "TRUE",
    "datetime": "NOW()",
    "uuid": "gen_random_uuid()",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestResult:
    """One generated-test outcome, keyed by foundry-test-id."""

    __test__ = False  # not a test case — pytest must not try to collect it

    name: str  # e.g. "endpoint:list_requests", "table:leave_requests"
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GateDecision:
    """Whether the approval gate may be offered for this build."""

    gate: str  # "preview_ready" | "blocked"
    approval_offered: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewCheck:
    """One slice-1 preview-checklist item, evaluated against the build."""

    name: str
    status: str  # "pass" | "fail" | "pending"
    detail: str = ""


@dataclass(frozen=True)
class Preview:
    """A disposable preview of a generated app — data only, never deployed."""

    app_name: str
    slug: str
    fingerprint: str  # spec fingerprint
    build_fingerprint: str
    fingerprint_shown: str  # first 12 chars of the spec fingerprint
    preview_token: str  # deterministic, derived from the build fingerprint
    expires_in_seconds: int
    pages: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    checks: tuple[PreviewCheck, ...] = ()
    health: str = "pending"  # "pending" | "passed" | "failed"
    gate: str = "blocked"  # "blocked" | "preview_ready"
    approval_offered: bool = False


@dataclass(frozen=True)
class SandboxVerification:
    """Outcome of running the generated schema in the disposable sandbox."""

    passed: bool
    tables_verified: tuple[str, ...] = ()
    rows_inserted: int = 0
    problems: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Test gate (pure)
# ---------------------------------------------------------------------------

def required_test_ids(spec: dict) -> tuple[str, ...]:
    """The test ids slice 3 requires for a spec: one per endpoint/table/page
    plus the two gate tests. Order is deterministic (spec order)."""
    ids: list[str] = []
    for endpoint in spec.get("endpoints", []) or []:
        ids.append(f"endpoint:{endpoint.get('name')}")
    for table in spec.get("tables", []) or []:
        ids.append(f"table:{table.get('name')}")
    for page in spec.get("pages", []) or []:
        ids.append(f"page:{page.get('path')}")
    ids.append("gate:approval_blocked_without_approval")
    ids.append("gate:preview_disposable_db")
    return tuple(ids)


def evaluate_test_results(required_ids, results) -> GateDecision:
    """Decide the test gate from results keyed by foundry-test-id.

    Any failed test blocks; any missing required test blocks. There is no
    partial pass: approval is offered only when every required test ran
    and passed.
    """
    required = tuple(required_ids)
    by_name = {r.name: r for r in results}
    reasons: list[str] = []
    for test_id in required:
        result = by_name.get(test_id)
        if result is None:
            reasons.append(f"missing test {test_id}")
        elif not result.passed:
            reasons.append(f"test {test_id} failed"
                           + (f": {result.detail}" if result.detail else ""))
    if reasons:
        return GateDecision(gate="blocked", approval_offered=False,
                            reasons=tuple(reasons))
    return GateDecision(gate="preview_ready", approval_offered=True, reasons=())


# ---------------------------------------------------------------------------
# Consistency (pure): generated files must agree with the spec
# ---------------------------------------------------------------------------

def check_generated_consistency(generated_app: GeneratedApp, spec: dict) -> tuple[str, ...]:
    """Check the generated files agree with the spec. Returns problems
    (empty = consistent): schema.sql and the migration draft must create
    exactly the declared tables, and the generated test file must carry
    every required test id."""
    problems: list[str] = []
    declared = {t.get("name") for t in spec.get("tables", []) or []}

    schema = generated_app.file(f"db/{generated_app.slug}/schema.sql")
    if schema is None:
        problems.append("missing schema.sql")
    else:
        created = set(_CREATE_TABLE_RE.findall(schema.content))
        if created != declared:
            problems.append(
                f"schema.sql creates {sorted(created)}, spec declares "
                f"{sorted(declared)}")

    migration = next(
        (f for f in generated_app.files if f.kind == "migration"), None)
    if migration is None:
        problems.append("missing migration draft")
    else:
        created = set(_MIGRATION_TABLE_RE.findall(migration.content))
        if created != declared:
            problems.append(
                f"migration draft creates {sorted(created)}, spec declares "
                f"{sorted(declared)}")

    test_file = next(
        (f for f in generated_app.files if f.kind == "test"), None)
    if test_file is None:
        problems.append("missing generated test file")
    else:
        for test_id in required_test_ids(spec):
            if f"foundry-test-id: {test_id}" not in test_file.content:
                problems.append(f"generated tests miss test id {test_id}")

    # Re-scan: generated content must still be clean at preview time.
    for generated in generated_app.files:
        for hit in scan_generated_content(generated.content):
            problems.append(f"{generated.path}: {hit}")
    return tuple(problems)


# ---------------------------------------------------------------------------
# Preview (pure)
# ---------------------------------------------------------------------------

def _preview_token(build_fingerprint: str) -> str:
    return hashlib.sha256(
        f"foundry-preview:{build_fingerprint}".encode("utf-8")).hexdigest()[:32]


def build_preview(generated_app: GeneratedApp, spec: dict,
                  validation: SpecValidation, *,
                  expires_in_seconds: int = MAX_PREVIEW_TTL_SECONDS,
                  health: str = "pending") -> Preview:
    """Build the preview record for a generated app, enforcing slice 1's
    preview checklist. The preview starts gated (`blocked`): `health` must
    be "passed" and the test gate must pass before `decide_gate()` can
    offer approval."""
    if not isinstance(validation, SpecValidation) or not validation.valid:
        raise ValueError("build_preview requires a valid SpecValidation.")
    if validation.build_plan is None:
        raise ValueError("build_preview requires a SpecValidation with a build plan.")
    if spec_fingerprint(spec) != validation.build_plan.fingerprint:
        raise ValueError("Spec fingerprint does not match the validation.")
    if generated_app.fingerprint != validation.build_plan.fingerprint:
        raise ValueError("Generated app was built from a different spec.")
    if compute_build_fingerprint(generated_app.files) != generated_app.build_fingerprint:
        raise ValueError("Generated app files changed after generation.")
    if health not in ("pending", "passed", "failed"):
        raise ValueError("health must be pending, passed or failed.")

    declared_tables = {t.get("name") for t in spec.get("tables", []) or []}
    schema = generated_app.file(f"db/{generated_app.slug}/schema.sql")
    schema_tables = (set(_CREATE_TABLE_RE.findall(schema.content))
                     if schema is not None else set())

    checks = (
        PreviewCheck(
            name="disposable_database",
            status=("pass" if schema is not None and schema_tables == declared_tables
                    and not any("DATABASE_URL" in f.content for f in generated_app.files)
                    else "fail"),
            detail="Preview database is disposable with seed data only; "
                   "no production data, no production connection string."),
        PreviewCheck(
            name="preview_link_expires",
            status=("pass" if 0 < expires_in_seconds <= MAX_PREVIEW_TTL_SECONDS
                    else "fail"),
            detail=f"Preview link expires after {expires_in_seconds}s "
                   f"(max {MAX_PREVIEW_TTL_SECONDS}s)."),
        PreviewCheck(
            name="fingerprint_shown",
            status="pass",
            detail=f"Preview shows build fingerprint "
                   f"{validation.build_plan.fingerprint[:12]}…."),
        PreviewCheck(
            name="lists_all_spec_items",
            status="pass",
            detail=f"Preview lists {len(spec.get('pages', []))} pages, "
                   f"{len(spec.get('endpoints', []))} endpoints, "
                   f"{len(spec.get('tables', []))} tables."),
        PreviewCheck(
            name="health_check",
            status=("pass" if health == "passed"
                    else "pending" if health == "pending" else "fail"),
            detail="Health check must pass before approval is offered."),
    )
    return Preview(
        app_name=generated_app.app_name,
        slug=generated_app.slug,
        fingerprint=generated_app.fingerprint,
        build_fingerprint=generated_app.build_fingerprint,
        fingerprint_shown=validation.build_plan.fingerprint[:12],
        preview_token=_preview_token(generated_app.build_fingerprint),
        expires_in_seconds=expires_in_seconds,
        pages=tuple(p.get("path") for p in spec.get("pages", []) or []),
        endpoints=tuple(e.get("name") for e in spec.get("endpoints", []) or []),
        tables=tuple(sorted(declared_tables)),
        checks=checks,
        health=health,
        gate="blocked",
        approval_offered=False,
    )


def decide_gate(preview: Preview, test_decision: GateDecision,
                sandbox: SandboxVerification | None) -> GateDecision:
    """Combine preview checks + test gate + sandbox health into one gate
    decision. Approval is offered only when every check passes, every
    required test passed, and the sandbox verification passed."""
    reasons: list[str] = []
    for check in preview.checks:
        if check.status != "pass":
            reasons.append(f"preview check {check.name} is {check.status}")
    if preview.health != "passed":
        reasons.append(f"health check is {preview.health}")
    if test_decision.gate != "preview_ready":
        reasons.extend(test_decision.reasons)
    if sandbox is None:
        reasons.append("no sandbox verification ran")
    elif not sandbox.passed:
        reasons.extend(sandbox.problems or ("sandbox verification failed",))
    if reasons:
        return GateDecision(gate="blocked", approval_offered=False,
                            reasons=tuple(reasons))
    return GateDecision(gate="preview_ready", approval_offered=True, reasons=())


def apply_gate(preview: Preview, decision: GateDecision) -> Preview:
    """Return a new Preview with the gate decision applied."""
    return replace(preview, gate=decision.gate,
                   approval_offered=decision.approval_offered)


def approval_offered(preview: Preview) -> bool:
    """True only when the preview's gate is preview_ready."""
    return preview.gate == "preview_ready" and preview.approval_offered


# ---------------------------------------------------------------------------
# Sandbox verification (executes generated schema.sql — disposable DB only)
# ---------------------------------------------------------------------------

def _seed_row_sql(table: dict) -> str:
    """One INSERT of seed values for a declared table (seed data only)."""
    columns = table.get("columns", []) or []
    names = ", ".join(f"\"{c['name']}\"" for c in columns)
    values = ", ".join(_SEED_LITERAL_BY_TYPE[c["type"]] for c in columns)
    return (f"INSERT INTO \"{table['name']}\" ({names}) VALUES ({values});")


def verify_in_sandbox(generated_app: GeneratedApp, spec: dict, pg_bin,
                      *, timeout: int = 180) -> SandboxVerification:
    """Run the generated schema.sql in a disposable PostgreSQL cluster and
    verify every declared table exists and accepts one seed row.

    Only ever runs against the disposable cluster created by
    `app.qa.sandbox.disposable_postgres` (127.0.0.1, throwaway database);
    the URL is checked before use and the password is passed via the
    environment, never logged. Any failure returns passed=False — it never
    raises for a bad schema, so the gate can block on the result.
    """
    from app.qa.sandbox import disposable_postgres  # lazy: stdlib-only chain

    problems: list[str] = []
    schema = generated_app.file(f"db/{generated_app.slug}/schema.sql")
    if schema is None:
        return SandboxVerification(passed=False, problems=("missing schema.sql",))
    tables = spec.get("tables", []) or []
    if not tables:
        return SandboxVerification(passed=False, problems=("spec declares no tables",))

    pg_bin = Path(pg_bin)
    psql = pg_bin / "psql"
    if not psql.is_file():
        return SandboxVerification(
            passed=False, problems=(f"psql not found under {pg_bin}",))

    try:
        with disposable_postgres(pg_bin) as env:
            test_url = env.get("TEST_DATABASE_URL", "")
            parsed = urlparse(test_url)
            # Guard: disposable loopback only — never any other database.
            if parsed.hostname != "127.0.0.1" or not parsed.path.strip("/"):
                return SandboxVerification(
                    passed=False,
                    problems=("refusing to run against a non-disposable database URL",))
            db_url = (f"postgresql://{parsed.username}@{parsed.hostname}:"
                      f"{parsed.port}{parsed.path}")
            run_env = dict(env)
            run_env["PGPASSWORD"] = parsed.password or ""

            with tempfile.TemporaryDirectory(prefix="foundry-schema-") as tmp:
                schema_path = Path(tmp) / "schema.sql"
                schema_path.write_text(schema.content, encoding="utf-8")
                applied = subprocess.run(
                    [str(psql), db_url, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-f", str(schema_path)],
                    env=run_env, capture_output=True, text=True, timeout=timeout)
            if applied.returncode != 0:
                return SandboxVerification(
                    passed=False,
                    problems=(f"schema.sql failed to apply in the sandbox: "
                              f"{(applied.stderr or '').strip()[:300]}",))

            verified: list[str] = []
            rows = 0
            for table in tables:
                name = table["name"]
                exists = subprocess.run(
                    [str(psql), db_url, "-X", "-q", "-t", "-A", "-c",
                     "SELECT count(*) FROM information_schema.tables "
                     f"WHERE table_name = '{name}';"],
                    env=run_env, capture_output=True, text=True, timeout=timeout)
                if exists.returncode != 0 or exists.stdout.strip() != "1":
                    problems.append(f"table {name} not found after applying schema.sql")
                    continue
                inserted = subprocess.run(
                    [str(psql), db_url, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-c", _seed_row_sql(table)],
                    env=run_env, capture_output=True, text=True, timeout=timeout)
                if inserted.returncode != 0:
                    problems.append(
                        f"seed row for table {name} failed: "
                        f"{(inserted.stderr or '').strip()[:200]}")
                    continue
                counted = subprocess.run(
                    [str(psql), db_url, "-X", "-q", "-t", "-A", "-c",
                     f"SELECT count(*) FROM \"{name}\";"],
                    env=run_env, capture_output=True, text=True, timeout=timeout)
                if counted.returncode != 0:
                    problems.append(f"could not count rows in table {name}")
                    continue
                rows += int(counted.stdout.strip())
                verified.append(name)
    except Exception as exc:  # sandbox failure blocks the gate, never crashes it
        return SandboxVerification(
            passed=False, problems=(f"sandbox verification error: {exc}",))

    if problems:
        return SandboxVerification(passed=False, tables_verified=tuple(verified),
                                   rows_inserted=rows, problems=tuple(problems))
    return SandboxVerification(passed=True, tables_verified=tuple(verified),
                               rows_inserted=rows, problems=())
