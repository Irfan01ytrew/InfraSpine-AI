"""Step AM (Self-Evolving Software Foundry), slice 2 — generator.

Turns a *validated* spec into a generated application — as DATA only.

`generate_app()` takes the spec dict plus the `SpecValidation` produced by
slice 1 (`app.foundry.spec.validate_spec`) and returns a `GeneratedApp`: a
deterministic set of generated files (interface page stubs, one FastAPI router
stub, an Alembic migration draft, a `schema.sql` for the QA sandbox, generated
test stubs, a README). Nothing is executed, written to disk, deployed, or
sent anywhere by this module — the output is plain data the later slices
(test + preview, approval gate, deploy) consume.

Safety, enforced structurally:
* Only a *valid* validation generates anything. An invalid validation, or a
  validation whose fingerprint does not match the spec, raises `ValueError`
  — a rejected spec can never reach the generator.
* Every generated file is re-scanned with slice 1's destructive/secret
  scanners plus a generator denylist (exec/eval, os.system, subprocess,
  sockets, network clients, absolute URLs, raw SQL statements). Any hit
  fails generation outright — unsafe code is never emitted.
* Generated code is data, not trusted code: interface stubs call only
  relative `/api/...` paths (no absolute URLs, no network hosts); backend
  stubs contain no SQL strings and no destructive operations; the migration
  draft is marked DRAFT and is never auto-applied; generated apps use demo /
  seed data only (no production data, no real credentials, no payment
  endpoints — those are rejected by slice 1 already).
* Output is deterministic: the same spec always produces the same files and
  the same build fingerprint, which slice 4 binds the deploy approval to.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.foundry.spec import (
    SpecValidation,
    find_destructive_patterns,
    find_secret_patterns,
    spec_fingerprint,
)

__all__ = [
    "GeneratedApp",
    "GeneratedFile",
    "compute_build_fingerprint",
    "generate_app",
    "scan_generated_content",
    "slugify_name",
]

# ---------------------------------------------------------------------------
# Type mapping (spec column type -> SQLAlchemy type / raw SQL type)
# ---------------------------------------------------------------------------

_SQLALCHEMY_TYPES = {
    "string": "String(255)",
    "text": "Text",
    "integer": "Integer",
    "number": "Float",
    "boolean": "Boolean",
    "datetime": "DateTime",
    "uuid": "UUID(as_uuid=True)",
}

_RAW_SQL_TYPES = {
    "string": "VARCHAR(255)",
    "text": "TEXT",
    "integer": "INTEGER",
    "number": "DOUBLE PRECISION",
    "boolean": "BOOLEAN",
    "datetime": "TIMESTAMP",
    "uuid": "UUID",
}

# Extra denylist for generated code. Slice 1's scanners already cover
# destructive statements and secrets; these cover execution / network /
# raw-SQL escape hatches the generator must never emit.
_FORBIDDEN_GENERATED = (
    re.compile(r"\bexec\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\b__import__\b"),
    re.compile(r"\bos\.system\b"),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"\bsocket\b"),
    re.compile(r"\brequests\."),
    re.compile(r"\burllib\b"),
    re.compile(r"\bhttpx\b"),
    re.compile(r"https?://"),
    re.compile(r"\bSELECT\b"),
    re.compile(r"\bINSERT\s+INTO\b"),
    re.compile(r"\bUPDATE\b\s+\w+\s+SET\b"),
    re.compile(r"\bDELETE\s+FROM\b"),
    re.compile(r"\bDROP\s+TABLE\b"),
    re.compile(r"\bTRUNCATE\b"),
    re.compile(r"\bGRANT\b|\bREVOKE\b"),
    re.compile(r"\bDATABASE_URL\b"),
    re.compile(r"\bMASTER_ENCRYPTION_KEY\b"),
)

# The migration draft legitimately contains `op.drop_table(...)` in its
# downgrade() (Alembic rollback, required by slice 1's test checklist). The
# denylist above only matches the SQL statement `DROP TABLE` (with a space),
# never the method call `drop_table(`, so the draft still scans clean.


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedFile:
    """One generated file, as data. Nothing here has been written anywhere."""

    path: str
    kind: str  # "interface" | "backend" | "migration" | "schema" | "test" | "docs"
    content: str


@dataclass(frozen=True)
class GeneratedApp:
    """A generated application: files + fingerprints, all as data."""

    app_name: str
    slug: str
    fingerprint: str  # spec fingerprint (slice 1) — the spec this was built from
    build_fingerprint: str  # SHA-256 of the canonical file set — the deploy
    # approval (slice 4) is bound to this exact value
    files: tuple[GeneratedFile, ...] = ()

    def file(self, path: str) -> GeneratedFile | None:
        """Return the generated file at `path`, or None."""
        for candidate in self.files:
            if candidate.path == path:
                return candidate
        return None

    def files_of_kind(self, kind: str) -> tuple[GeneratedFile, ...]:
        """All generated files of one kind, in path order."""
        return tuple(f for f in self.files if f.kind == kind)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify_name(name: str) -> str:
    """Turn an app name into a safe slug: lower-case, spaces to underscores."""
    slug = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower().replace(" ", "_"))
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "app"


def _page_slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", path.strip("/").lower()).strip("_") or "home"


def _component_name(page_name: str) -> str:
    words = re.split(r"[^A-Za-z0-9]+", page_name.strip())
    return "".join(w.capitalize() for w in words if w) + "Page"


def scan_generated_content(content: str) -> tuple[str, ...]:
    """Return the forbidden patterns found in generated `content` (empty = clean).

    Combines slice 1's destructive/secret scanners with the generator
    denylist. Any non-empty result means the content must never be emitted.
    """
    hits: list[str] = []
    for pattern in find_destructive_patterns(content):
        hits.append(f"destructive:{pattern}")
    for pattern in find_secret_patterns(content):
        hits.append(f"secret:{pattern}")
    if not isinstance(content, str):
        return tuple(hits)
    for pattern in _FORBIDDEN_GENERATED:
        if pattern.search(content):
            hits.append(f"forbidden:{pattern.pattern}")
    return tuple(hits)


def compute_build_fingerprint(files: tuple[GeneratedFile, ...]) -> str:
    """SHA-256 of the canonical JSON of the file set (sorted by path)."""
    canonical = json.dumps(
        [{"path": f.path, "content": f.content} for f in sorted(files, key=lambda x: x.path)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# File builders (each returns file content as text — nothing is executed)
# ---------------------------------------------------------------------------

def _interface_file(app_slug: str, page: dict, spec: dict) -> GeneratedFile:
    path = f"frontend/src/foundry/{app_slug}/{_page_slug(page['path'])}.tsx"
    component = _component_name(page.get("name", "Page"))
    endpoint_calls = []
    for endpoint_name in page.get("endpoints", []) or []:
        endpoint = next(
            (e for e in spec.get("endpoints", []) if e.get("name") == endpoint_name), None)
        if endpoint is not None:
            endpoint_calls.append(
                f'  // {endpoint["method"]} {endpoint["path"]} '
                f"({endpoint_name}) — relative path only, same origin.\n"
                f"  const {endpoint_name} = async () => fetch(\"{endpoint['path']}\");")
    calls = "\n".join(endpoint_calls) if endpoint_calls else "  // No endpoints on this page."
    content = (
        f"// GENERATED — Self-Evolving Software Foundry. Do not edit by hand;\n"
        f"// regenerate from the app spec. Calls only relative /api paths.\n"
        f"export function {component}() {{\n"
        f"{calls}\n"
        f"  return (\n"
        f"    <main>\n"
        f"      <h1>{page.get('name', 'Page')}</h1>\n"
        f"    </main>\n"
        f"  );\n"
        f"}}\n"
    )
    return GeneratedFile(path=path, kind="interface", content=content)


def _backend_file(app_slug: str, spec: dict) -> GeneratedFile:
    path = f"app/foundry_generated/{app_slug}/router.py"
    handlers = []
    for endpoint in spec.get("endpoints", []):
        table = endpoint.get("table")
        table_note = f", \"table\": \"{table}\"" if table else ""
        handlers.append(
            f"@router.{endpoint['method'].lower()}("
            f"\"{endpoint['path']}\")\n"
            f"async def {endpoint['name']}():\n"
            f"    # GENERATED stub — wire the real handler through app/platform\n"
            f"    # (authorisation, approvals, audit) before this ships.\n"
            f"    return {{\"ok\": True, \"endpoint\": \"{endpoint['name']}\"{table_note}}}"
        )
    body = "\n\n\n".join(handlers) if handlers else "# No endpoints in this spec."
    content = (
        "# GENERATED — Self-Evolving Software Foundry. Stub router, as data.\n"
        "# Real handlers must go through app/platform; this file is not\n"
        "# executed by the Foundry and is never deployed without approval.\n"
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        f"{body}\n"
    )
    return GeneratedFile(path=path, kind="backend", content=content)


def _migration_file(app_slug: str, fingerprint: str, spec: dict) -> GeneratedFile:
    revision = fingerprint[:12]
    path = f"alembic/versions/{revision}_foundry_{app_slug}.py"
    creates = []
    for table in spec.get("tables", []):
        columns = []
        for column in table.get("columns", []):
            ctype = _SQLALCHEMY_TYPES[column["type"]]
            pk = ", primary_key=True" if column["name"] == "id" else ""
            columns.append(
                f"        sa.Column(\"{column['name']}\", sa.{ctype}, nullable=True{pk}),")
        columns_text = "\n".join(columns)
        creates.append(
            f"    op.create_table(\n"
            f"        \"{table['name']}\",\n"
            f"{columns_text}\n"
            f"    )")
    drops = "\n".join(
        f"    op.drop_table(\"{table['name']}\")"
        for table in reversed(spec.get("tables", [])))
    content = (
        "# GENERATED DRAFT — Self-Evolving Software Foundry.\n"
        "# Never auto-applied. Review it, then apply via Alembic only after\n"
        "# the build is approved. Rollback drops the tables in reverse order.\n"
        f"revision = \"{revision}\"\n"
        "down_revision = None\n"
        "\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "\n"
        "\n"
        "def upgrade():\n"
        + ("\n".join(creates) if creates else "    pass")
        + "\n"
        "\n"
        "\n"
        "def downgrade():\n"
        + (drops if drops else "    pass")
        + "\n"
    )
    return GeneratedFile(path=path, kind="migration", content=content)


def _schema_file(app_slug: str, spec: dict) -> GeneratedFile:
    path = f"db/{app_slug}/schema.sql"
    statements = [
        "-- GENERATED schema for the QA sandbox only. Seed data only;",
        "-- never production data, never applied outside a disposable DB.",
    ]
    for table in spec.get("tables", []):
        columns = []
        for column in table.get("columns", []):
            sql_type = _RAW_SQL_TYPES[column["type"]]
            pk = " PRIMARY KEY" if column["name"] == "id" else ""
            columns.append(f"    \"{column['name']}\" {sql_type}{pk}")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS \"{table['name']}\" (\n"
            + ",\n".join(columns)
            + "\n);")
    return GeneratedFile(
        path=path, kind="schema", content="\n".join(statements) + "\n")


def _generated_tests_file(app_slug: str, spec: dict) -> GeneratedFile:
    path = f"tests/foundry/{app_slug}/test_{app_slug}_generated.py"
    lines = [
        "# GENERATED test stubs — Self-Evolving Software Foundry.",
        "# Each test carries its foundry-test-id; slice 3 runs these in the",
        "# QA sandbox and blocks the approval gate on any failure.",
        "",
    ]
    for endpoint in spec.get("endpoints", []):
        lines.append(f"# foundry-test-id: endpoint:{endpoint['name']}")
        lines.append(f"def test_endpoint_{endpoint['name']}():")
        lines.append(f"    # {endpoint['method']} {endpoint['path']}")
        lines.append("    assert True  # replaced by the sandbox run")
        lines.append("")
    for table in spec.get("tables", []):
        lines.append(f"# foundry-test-id: table:{table['name']}")
        lines.append(f"def test_table_{table['name']}():")
        lines.append("    # migration applies and rolls back in the sandbox")
        lines.append("    assert True  # replaced by the sandbox run")
        lines.append("")
    for page in spec.get("pages", []):
        test_name = _page_slug(page["path"])
        lines.append(f"# foundry-test-id: page:{page['path']}")
        lines.append(f"def test_page_{test_name}():")
        lines.append(f"    # page {page['path']} renders")
        lines.append("    assert True  # replaced by the sandbox run")
        lines.append("")
    lines.append("# foundry-test-id: gate:approval_blocked_without_approval")
    lines.append("def test_gate_approval_blocked_without_approval():")
    lines.append("    assert True  # replaced by the sandbox run")
    lines.append("")
    lines.append("# foundry-test-id: gate:preview_disposable_db")
    lines.append("def test_gate_preview_disposable_db():")
    lines.append("    assert True  # replaced by the sandbox run")
    lines.append("")
    return GeneratedFile(path=path, kind="test", content="\n".join(lines))


def _readme_file(app_slug: str, spec: dict, fingerprint: str) -> GeneratedFile:
    path = f"foundry/{app_slug}/README.md"
    lines = [
        f"# {spec.get('name', app_slug)} — generated app (Foundry)",
        "",
        f"Spec fingerprint: `{fingerprint}`",
        "",
        "Generated as data only. Nothing here has been executed, deployed,",
        "or applied. Test + preview (slice 3) run in the QA sandbox; deploy",
        "needs the owner's approval of the exact build fingerprint (slice 4).",
        "",
        "## Pages",
        "",
    ]
    for page in spec.get("pages", []):
        lines.append(f"- {page.get('name')} — `{page.get('path')}`")
    lines += ["", "## Endpoints", ""]
    for endpoint in spec.get("endpoints", []):
        lines.append(
            f"- {endpoint.get('method')} `{endpoint.get('path')}` "
            f"({endpoint.get('name')})")
    lines += ["", "## Tables", ""]
    for table in spec.get("tables", []):
        cols = ", ".join(c["name"] for c in table.get("columns", []))
        lines.append(f"- {table.get('name')} ({cols})")
    lines.append("")
    return GeneratedFile(path=path, kind="docs", content="\n".join(lines))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate_app(spec: dict, validation: SpecValidation) -> GeneratedApp:
    """Generate an application from a validated spec. Returns data only.

    Raises ValueError when the validation is not a valid slice-1 validation
    for this exact spec — a rejected or mismatched spec never generates.
    Raises ValueError when any generated file fails the safety re-scan.
    """
    if not isinstance(validation, SpecValidation) or not validation.valid:
        raise ValueError(
            "generate_app requires a valid SpecValidation; the spec was "
            "rejected by slice 1 and must not be generated.")
    if validation.build_plan is None:
        raise ValueError("generate_app requires a SpecValidation with a build plan.")
    if not isinstance(spec, dict):
        raise ValueError("generate_app requires the spec dict that was validated.")
    if spec_fingerprint(spec) != validation.build_plan.fingerprint:
        raise ValueError(
            "Spec fingerprint does not match the validation: the spec changed "
            "after validation — re-validate before generating.")

    slug = slugify_name(validation.build_plan.app_name)
    fingerprint = validation.build_plan.fingerprint

    files: list[GeneratedFile] = []
    for page in spec.get("pages", []):
        files.append(_interface_file(slug, page, spec))
    files.append(_backend_file(slug, spec))
    files.append(_migration_file(slug, fingerprint, spec))
    files.append(_schema_file(slug, spec))
    files.append(_generated_tests_file(slug, spec))
    files.append(_readme_file(slug, spec, fingerprint))
    files.sort(key=lambda f: f.path)

    # Fail closed: re-scan everything before emitting it.
    problems: list[str] = []
    for generated in files:
        for hit in scan_generated_content(generated.content):
            problems.append(f"{generated.path}: {hit}")
    if problems:
        raise ValueError(
            "Generated content failed the safety re-scan and was not emitted: "
            + "; ".join(problems))

    return GeneratedApp(
        app_name=validation.build_plan.app_name,
        slug=slug,
        fingerprint=fingerprint,
        build_fingerprint=compute_build_fingerprint(tuple(files)),
        files=tuple(files),
    )
