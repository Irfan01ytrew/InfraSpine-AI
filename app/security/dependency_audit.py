"""Dependency vulnerability audit (Step I slice 7) — pure parsing/filtering.

The scanners themselves (``pip-audit`` and ``npm audit``) need the network;
this module only parses their JSON output and applies the allowlist, so it
is unit-testable offline. The CLI wrapper is
``scripts/security_dependency_audit.py``; the gate lives in
``.github/workflows/ci.yml`` (job ``security-audit``).

Exit-code contract of the CLI (see the wrapper): 0 = clean or every
finding allowlisted; 1 = unallowlisted findings; 2 = a scanner could not
run (missing tool, no network, malformed output) — fail closed, never a
silent green.

An allowlist entry matches one finding exactly (ecosystem + package +
advisory id). Entries expire: an expired entry no longer allows its
finding and is reported separately, so stale acceptances fail loudly
instead of lingering forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    """One advisory hit from a scanner."""

    id: str
    package: str
    ecosystem: str  # "pip" or "npm"
    fixed_versions: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class AllowEntry:
    """One accepted vulnerability (temporary, with a reason and an expiry)."""

    id: str
    package: str
    ecosystem: str
    reason: str
    expires: date

    def matches(self, finding: Finding) -> bool:
        return (
            self.ecosystem == finding.ecosystem
            and self.package == finding.package
            and self.id == finding.id
        )

    def is_expired(self, today: date) -> bool:
        return self.expires < today


@dataclass
class AuditResult:
    """The outcome of applying the allowlist to scanner findings."""

    unallowlisted: list[Finding] = field(default_factory=list)
    allowed: list[Finding] = field(default_factory=list)
    expired_entries: list[AllowEntry] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.unallowlisted and not self.expired_entries


def parse_pip_audit(data: dict[str, Any]) -> list[Finding]:
    """Parse ``pip-audit --format json`` output into findings.

    Shape: ``{"dependencies": [{"name": ..., "version": ...,
    "vulns": [{"id": "PYSEC-...", "fix_versions": [...],
    "description": ...}]}]}``. Dependencies without ``vulns`` (or with an
    empty list) produce no findings. Malformed entries are skipped, never
    invented.
    """
    findings: list[Finding] = []
    if not isinstance(data, dict):
        return findings
    for dep in data.get("dependencies", []) or []:
        if not isinstance(dep, dict):
            continue
        name = dep.get("name")
        if not name:
            continue
        for vuln in dep.get("vulns", []) or []:
            if not isinstance(vuln, dict) or not vuln.get("id"):
                continue
            fix_versions = vuln.get("fix_versions") or []
            findings.append(
                Finding(
                    id=str(vuln["id"]),
                    package=str(name),
                    ecosystem="pip",
                    fixed_versions=tuple(str(v) for v in fix_versions),
                    description=str(vuln.get("description") or ""),
                )
            )
    return findings


def _npm_advisory_ids(via: Any) -> list[str]:
    """Advisory ids from an ``npm audit`` ``via`` list.

    Entries are dicts with a ``url`` (``.../GHSA-xxxx``) or plain strings
    naming a transitive dependency (no advisory of their own).
    """
    ids: list[str] = []
    for entry in via or []:
        if isinstance(entry, dict):
            url = entry.get("url") or ""
            advisory = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
            if advisory:
                ids.append(advisory)
        elif isinstance(entry, str):
            continue
    return ids


def parse_npm_audit(data: dict[str, Any]) -> list[Finding]:
    """Parse ``npm audit --json`` output into findings.

    Shape: ``{"vulnerabilities": {name: {"via": [...],
    "fixAvailable": false | true | {"version": ...}, "severity": ...}}}``.
    One finding per advisory id; ``fixAvailable`` records the version that
    fixes it when the scanner names one.
    """
    findings: list[Finding] = []
    if not isinstance(data, dict):
        return findings
    vulnerabilities = data.get("vulnerabilities", {}) or {}
    if not isinstance(vulnerabilities, dict):
        return findings
    for name, details in vulnerabilities.items():
        if not isinstance(details, dict):
            continue
        fix = details.get("fixAvailable")
        fixed: tuple[str, ...] = ()
        if isinstance(fix, dict) and fix.get("version"):
            fixed = (str(fix["version"]),)
        for advisory_id in _npm_advisory_ids(details.get("via")):
            findings.append(
                Finding(
                    id=advisory_id,
                    package=str(name),
                    ecosystem="npm",
                    fixed_versions=fixed,
                    description="",
                )
            )
    return findings


def load_allowlist(path: str | Path) -> list[AllowEntry]:
    """Load and validate the allowlist file.

    Shape: ``{"allowlist": [{"id", "package", "ecosystem", "reason",
    "expires": "YYYY-MM-DD"}]}``. Any malformed entry raises
    :class:`ValueError` — a broken allowlist must fail the gate, never
    silently allow everything.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or "allowlist" not in raw
        or not isinstance(raw["allowlist"], list)
    ):
        raise ValueError("allowlist file must be an object with an 'allowlist' list")
    entries: list[AllowEntry] = []
    for item in raw["allowlist"]:
        if not isinstance(item, dict):
            raise ValueError("allowlist entry is not an object")
        for key in ("id", "package", "ecosystem", "reason", "expires"):
            if not item.get(key):
                raise ValueError(f"allowlist entry is missing '{key}': {item!r}")
        if item["ecosystem"] not in ("pip", "npm"):
            raise ValueError(f"allowlist entry has unknown ecosystem: {item['ecosystem']!r}")
        try:
            expires = date.fromisoformat(str(item["expires"]))
        except ValueError as exc:
            raise ValueError(f"allowlist entry has a bad expiry date: {item['expires']!r}") from exc
        entries.append(
            AllowEntry(
                id=str(item["id"]),
                package=str(item["package"]),
                ecosystem=str(item["ecosystem"]),
                reason=str(item["reason"]),
                expires=expires,
            )
        )
    return entries


def apply_allowlist(
    findings: list[Finding],
    allowlist: list[AllowEntry],
    today: date | None = None,
) -> AuditResult:
    """Split findings into allowlisted and unallowlisted; flag expired entries."""
    today = today or date.today()
    result = AuditResult()
    expired = [entry for entry in allowlist if entry.is_expired(today)]
    result.expired_entries = expired
    live = [entry for entry in allowlist if not entry.is_expired(today)]
    for finding in findings:
        if any(entry.matches(finding) for entry in live):
            result.allowed.append(finding)
        else:
            result.unallowlisted.append(finding)
    return result
