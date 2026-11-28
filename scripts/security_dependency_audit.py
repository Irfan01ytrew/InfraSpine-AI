#!/usr/bin/env python3
"""Dependency vulnerability audit gate (Step I slice 7).

Runs ``pip-audit`` against the Python lockfile and ``npm audit`` against
the frontend, then applies the allowlist
(``docs/security-audit-allowlist.json``).

Exit codes: 0 = clean, or every finding is allowlisted; 1 = unallowlisted
findings (or expired allowlist entries); 2 = a scanner could not run
(missing tool, no network, malformed output) — fail closed, never a
silent green. In CI the ``security-audit`` job runs this script; locally,
install the scanners first (``pip install pip-audit``; ``npm`` ships
with Node).

Usage:
    python scripts/security_dependency_audit.py [--skip-pip] [--skip-npm]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load the pure logic directly from its file instead of importing the
# ``app`` package: the script must run with any Python that has pip-audit
# (in CI the security-audit job installs only pip-audit, not the app's
# dependencies), and importing ``app.security`` would pull in fastapi via
# its ``__init__``. The module itself is stdlib-only.
_spec = importlib.util.spec_from_file_location(
    "dependency_audit", REPO_ROOT / "app" / "security" / "dependency_audit.py"
)
_dep = importlib.util.module_from_spec(_spec)
sys.modules["dependency_audit"] = _dep  # dataclasses looks the module up here
_spec.loader.exec_module(_dep)
AuditResult = _dep.AuditResult
apply_allowlist = _dep.apply_allowlist
load_allowlist = _dep.load_allowlist
parse_npm_audit = _dep.parse_npm_audit
parse_pip_audit = _dep.parse_pip_audit

DEFAULT_REQUIREMENTS = REPO_ROOT / "requirements.lock"
DEFAULT_FRONTEND = REPO_ROOT / "frontend"
DEFAULT_ALLOWLIST = REPO_ROOT / "docs" / "security-audit-allowlist.json"


def _scanner_env() -> dict:
    """Environment for the scanners.

    pip-audit builds a scratch virtualenv under the temp dir; on this VM
    /tmp is a 512 MB tmpfs and the build fails with "No space left on
    device", so when the temp dir has less than 1 GB free the scanners get
    a temp dir under the user's home instead. On CI /tmp is large and
    nothing changes.
    """
    env = dict(os.environ)
    tmp = Path(tempfile.gettempdir())
    try:
        free = shutil.disk_usage(tmp).free
    except OSError:
        free = 0
    if free < 1_073_741_824:
        home_tmp = Path.home() / "tmp"
        home_tmp.mkdir(exist_ok=True)
        env["TMPDIR"] = str(home_tmp)
    return env


def _run(command: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        # pip-audit resolves the whole lockfile before it queries the
        # advisory DB; under a loaded VM that takes well over 10 minutes.
        # The CI job has its own (shorter) timeout.
        timeout=1800,
        env=_scanner_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _audit_pip(requirements: Path) -> tuple[list, str | None]:
    """Return (findings, error). ``error`` is set when the scan could not run."""
    try:
        rc, out, err = _run(
            [sys.executable, "-m", "pip_audit", "-r", str(requirements), "--format", "json"]
        )
    except FileNotFoundError:
        return [], "pip-audit is not installed (pip install pip-audit)"
    except subprocess.TimeoutExpired:
        return [], "pip-audit timed out"
    if rc not in (0, 1):
        # pip-audit exits 1 when it finds vulnerabilities; anything else
        # (e.g. no network for the advisory DB) means the scan did not run.
        detail = (err or out).strip().splitlines()
        return [], f"pip-audit failed (exit {rc}): {detail[-1] if detail else 'no output'}"
    try:
        return parse_pip_audit(json.loads(out)), None
    except (json.JSONDecodeError, ValueError) as exc:
        return [], f"pip-audit output was not valid JSON: {exc}"


def _audit_npm(frontend_dir: Path) -> tuple[list, str | None]:
    """Return (findings, error). ``error`` is set when the scan could not run."""
    if not (frontend_dir / "package-lock.json").exists():
        return [], f"no package-lock.json in {frontend_dir}"
    try:
        rc, out, err = _run(["npm", "audit", "--json"], cwd=frontend_dir)
    except FileNotFoundError:
        return [], "npm is not installed"
    except subprocess.TimeoutExpired:
        return [], "npm audit timed out"
    # npm audit exits non-zero when it finds vulnerabilities; it still
    # prints JSON on stdout. Only a missing/empty stdout means no scan.
    if not out.strip():
        detail = (err or "").strip().splitlines()
        return [], f"npm audit produced no output (exit {rc}): {detail[-1] if detail else 'no output'}"
    try:
        return parse_npm_audit(json.loads(out)), None
    except (json.JSONDecodeError, ValueError) as exc:
        return [], f"npm audit output was not valid JSON: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dependency vulnerability audit gate.")
    parser.add_argument("--requirements", default=str(DEFAULT_REQUIREMENTS))
    parser.add_argument("--frontend-dir", default=str(DEFAULT_FRONTEND))
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--skip-pip", action="store_true")
    parser.add_argument("--skip-npm", action="store_true")
    args = parser.parse_args(argv)

    try:
        allowlist = load_allowlist(args.allowlist)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"allowlist error: {exc}")
        return 2

    findings: list = []
    errors: list[str] = []
    if not args.skip_pip:
        pip_findings, error = _audit_pip(Path(args.requirements))
        findings.extend(pip_findings)
        if error:
            errors.append(error)
    if not args.skip_npm:
        npm_findings, error = _audit_npm(Path(args.frontend_dir))
        findings.extend(npm_findings)
        if error:
            errors.append(error)

    result: AuditResult = apply_allowlist(findings, allowlist, today=date.today())
    print(f"findings: {len(findings)} total, "
          f"{len(result.allowed)} allowlisted, "
          f"{len(result.unallowlisted)} unallowlisted")
    for finding in result.unallowlisted:
        fixed = f" (fixed in {', '.join(finding.fixed_versions)})" if finding.fixed_versions else ""
        print(f"  FAIL {finding.ecosystem}:{finding.package} {finding.id}{fixed}")
    for entry in result.expired_entries:
        print(f"  EXPIRED allowlist entry: {entry.ecosystem}:{entry.package} "
              f"{entry.id} (expired {entry.expires}; reason: {entry.reason})")
    if errors:
        # A scanner that could not run fails the gate, but the findings
        # the other scanner did produce are still reported above.
        for error in errors:
            print(f"scan unavailable: {error}")
        return 2
    if result.is_clean:
        print("dependency audit: clean")
        return 0
    print("dependency audit: FAILED — fix, pin, or allowlist the findings above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
