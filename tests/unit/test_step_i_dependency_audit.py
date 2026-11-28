"""Step I slice 7: dependency-audit parsing + allowlist (pure, offline).

The scanners themselves need the network; these tests cover only the
parsing and allowlist logic with fabricated scanner output.
"""

import json
from datetime import date

import pytest

from app.security.dependency_audit import (
    AllowEntry,
    Finding,
    apply_allowlist,
    load_allowlist,
    parse_npm_audit,
    parse_pip_audit,
)

TODAY = date(2026, 11, 28)


# --------------------------------------------------------------------------- pip-audit parsing


def test_parse_pip_audit_finds_vulns_only():
    data = {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.31.0",
                "vulns": [
                    {"id": "PYSEC-2023-1", "fix_versions": ["2.32.0"], "description": "x"},
                    {"id": "PYSEC-2023-2", "fix_versions": [], "description": "y"},
                ],
            },
            {"name": "clean-pkg", "version": "1.0.0", "vulns": []},
            {"name": "no-vulns-key", "version": "1.0.0"},
        ]
    }
    findings = parse_pip_audit(data)
    assert [(f.id, f.package, f.ecosystem) for f in findings] == [
        ("PYSEC-2023-1", "requests", "pip"),
        ("PYSEC-2023-2", "requests", "pip"),
    ]
    assert findings[0].fixed_versions == ("2.32.0",)
    assert findings[1].fixed_versions == ()


def test_parse_pip_audit_skips_malformed_entries():
    assert parse_pip_audit({}) == []
    assert parse_pip_audit({"dependencies": "not-a-list"}) == []
    assert parse_pip_audit({"dependencies": [{"vulns": [{"id": "PYSEC-1"}]}]}) == []
    assert parse_pip_audit({"dependencies": [{"name": "x", "vulns": [{"no_id": 1}]}]}) == []


# --------------------------------------------------------------------------- npm audit parsing


def test_parse_npm_audit_collects_advisory_ids():
    data = {
        "vulnerabilities": {
            "lodash": {
                "via": [
                    {"url": "https://github.com/advisories/GHSA-xxxx-yyyy"},
                    "transitive-dep",
                ],
                "fixAvailable": {"name": "lodash", "version": "4.17.21", "isSemVerMajor": False},
            },
            "clean-pkg": {"via": ["other-pkg"], "fixAvailable": False},
        }
    }
    findings = parse_npm_audit(data)
    assert len(findings) == 1
    assert findings[0].id == "GHSA-xxxx-yyyy"
    assert findings[0].package == "lodash"
    assert findings[0].ecosystem == "npm"
    assert findings[0].fixed_versions == ("4.17.21",)


def test_parse_npm_audit_no_fix_version_when_none_available():
    data = {
        "vulnerabilities": {
            "pkg": {
                "via": [{"url": "https://github.com/advisories/GHSA-1"}],
                "fixAvailable": False,
            }
        }
    }
    assert parse_npm_audit(data)[0].fixed_versions == ()
    assert parse_npm_audit({}) == []
    assert parse_npm_audit({"vulnerabilities": []}) == []


# --------------------------------------------------------------------------- allowlist loading


def test_load_allowlist_valid(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "allowlist": [
                    {
                        "id": "PYSEC-1",
                        "package": "requests",
                        "ecosystem": "pip",
                        "reason": "no fix released yet; not reachable",
                        "expires": "2027-01-31",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = load_allowlist(path)
    assert entries == [
        AllowEntry(
            id="PYSEC-1",
            package="requests",
            ecosystem="pip",
            reason="no fix released yet; not reachable",
            expires=date(2027, 1, 31),
        )
    ]


def test_load_allowlist_rejects_malformed(tmp_path):
    bad_cases = [
        {"allowlist": [{"id": "PYSEC-1"}]},  # missing keys
        {"allowlist": [{"id": "X", "package": "p", "ecosystem": "gem", "reason": "r", "expires": "2027-01-01"}]},
        {"allowlist": [{"id": "X", "package": "p", "ecosystem": "pip", "reason": "r", "expires": "not-a-date"}]},
        {"not-allowlist": []},
    ]
    for i, bad in enumerate(bad_cases):
        path = tmp_path / f"bad{i}.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            load_allowlist(path)


# --------------------------------------------------------------------------- allowlist application


def _entry(**overrides):
    base = {
        "id": "PYSEC-1",
        "package": "requests",
        "ecosystem": "pip",
        "reason": "accepted for now",
        "expires": date(2027, 1, 31),
    }
    base.update(overrides)
    return AllowEntry(**base)


def test_apply_allowlist_allows_exact_match_only():
    finding = Finding(id="PYSEC-1", package="requests", ecosystem="pip")
    result = apply_allowlist([finding], [_entry()], today=TODAY)
    assert result.allowed == [finding]
    assert result.unallowlisted == []
    assert result.is_clean

    # Wrong id / package / ecosystem does not allow.
    for entry in (
        _entry(id="PYSEC-2"),
        _entry(package="urllib3"),
        _entry(ecosystem="npm"),
    ):
        result = apply_allowlist([finding], [entry], today=TODAY)
        assert result.unallowlisted == [finding]
        assert result.allowed == []


def test_apply_allowlist_expired_entry_fails_loudly():
    finding = Finding(id="PYSEC-1", package="requests", ecosystem="pip")
    entry = _entry(expires=date(2026, 11, 1))  # before TODAY
    result = apply_allowlist([finding], [entry], today=TODAY)
    assert result.unallowlisted == [finding]
    assert result.expired_entries == [entry]
    assert not result.is_clean


def test_apply_allowlist_empty_is_clean():
    assert apply_allowlist([], [], today=TODAY).is_clean
