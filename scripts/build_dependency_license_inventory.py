"""Build a source-based dependency/license inventory from Python requirements and npm lock data.

The Python project has no transitive lockfile. This report records declared Python
requirements and the metadata visible in the verification environment, while the
frontend inventory is complete for the checked-in npm package lock.
"""

from __future__ import annotations

import argparse
import json
import re
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version


REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-reporting.txt",
    "requirements-media.txt",
    "requirements-document-tables.txt",
    "requirements-dev.txt",
    "requirements-browser.txt",
)

# Used only when an optional package is not installed in the local verification
# environment. URLs point to the pinned release metadata; ambiguous labels remain
# explicitly flagged for legal review rather than being guessed into an SPDX id.
PYTHON_METADATA_FALLBACKS: dict[tuple[str, str], dict[str, Any]] = {
    ("arabic-reshaper", "3.0.1"): {
        "license": "MIT",
        "source": "https://pypi.org/pypi/arabic-reshaper/3.0.1/json",
        "manual_review": False,
    },
    ("python-bidi", "0.6.12"): {
        "license": "LGPL (PyPI classifier; exact SPDX variant unconfirmed)",
        "source": "https://pypi.org/pypi/python-bidi/0.6.12/json",
        "manual_review": True,
    },
    ("pdfplumber", "0.11.10"): {
        "license": "MIT (PyPI OSI classifier; release lists LICENSE.txt)",
        "source": "https://pypi.org/pypi/pdfplumber/0.11.10/json",
        "manual_review": False,
    },
}


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_python_requirements(root: Path) -> list[dict[str, Any]]:
    """Read direct entries from the project's declared requirements sets."""
    collected: dict[str, dict[str, Any]] = {}
    visited: set[tuple[Path, str]] = set()
    requirement_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[([^\]]+)\])?\s*(.*)$")
    include_pattern = re.compile(r"^\s*-r\s+(.+?)\s*$", re.IGNORECASE)

    def read_file(relative: str, group: str) -> None:
        path = (root / relative).resolve()
        visit_key = (path, group)
        if visit_key in visited or not path.is_file():
            return
        visited.add(visit_key)
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            include = include_pattern.match(line)
            if include:
                child = (path.parent / include.group(1)).resolve()
                try:
                    child_relative = child.relative_to(root.resolve()).as_posix()
                except ValueError:
                    raise ValueError(f"Requirement include escapes the source root: {relative}") from None
                read_file(child_relative, group)
                continue
            if line.startswith(("-", "--")):
                continue
            match = requirement_pattern.match(line)
            if not match:
                raise ValueError(f"Unsupported requirement syntax in {relative}")
            name, extras_text, specifier = match.groups()
            key = _normalize_name(name)
            record = collected.setdefault(
                key,
                {
                    "name": name,
                    "declared_specifier": specifier.strip() or "*",
                    "requirement_files": set(),
                    "extras": set(),
                },
            )
            if record["declared_specifier"] != (specifier.strip() or "*"):
                record["declared_specifier"] = "; ".join(
                    sorted({record["declared_specifier"], specifier.strip() or "*"})
                )
            record["requirement_files"].add(relative)
            record["requirement_files"].add(group)
            record["extras"].update(extra.strip() for extra in (extras_text or "").split(",") if extra.strip())

    for filename in REQUIREMENT_FILES:
        read_file(filename, filename)

    result = []
    for key in sorted(collected):
        record = collected[key]
        result.append(
            {
                "name": record["name"],
                "declared_specifier": record["declared_specifier"],
                "requirement_files": sorted(record["requirement_files"]),
                "requested_extras": sorted(record["extras"]),
            }
        )
    return result


def read_npm_lock_packages(root: Path) -> list[dict[str, Any]]:
    package_json = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    declared = {
        _normalize_name(name): "runtime" for name in package_json.get("dependencies", {})
    }
    declared.update(
        {_normalize_name(name): "development" for name in package_json.get("devDependencies", {})}
    )
    packages: list[dict[str, Any]] = []
    for key, item in lock.get("packages", {}).items():
        if not key.startswith("node_modules/"):
            continue
        name = key.removeprefix("node_modules/")
        normalized = _normalize_name(name.rsplit("/node_modules/", 1)[-1])
        packages.append(
            {
                "name": name.rsplit("/node_modules/", 1)[-1],
                "version": item.get("version"),
                "license": item.get("license") or "MISSING",
                "dependency_class": declared.get(normalized, "transitive"),
                "optional": bool(item.get("optional")),
                "license_metadata_present": bool(item.get("license")),
            }
        )
    return sorted(packages, key=lambda package: (package["name"].lower(), package["version"] or ""))


def _python_license(name: str, specifier: str, installed_version: str | None) -> dict[str, Any]:
    fallback = None
    exact_pin = specifier[2:] if specifier.startswith("==") else None
    if installed_version is None or not installed_version:
        if exact_pin:
            fallback = PYTHON_METADATA_FALLBACKS.get((_normalize_name(name), exact_pin))
        return {
            "installed_version": None,
            "version_satisfies_declared_specifier": False,
            "license": fallback["license"] if fallback else "MISSING",
            "license_source": fallback["source"] if fallback else None,
            "manual_review": fallback["manual_review"] if fallback else True,
        }

    license_text = ""
    license_source = "installed distribution metadata"
    manual_review = False
    try:
        distribution = metadata.distribution(name)
        expression = distribution.metadata.get("License-Expression")
        declared = distribution.metadata.get("License")
        license_text = re.sub(r"\s+", " ", (expression or declared or "").strip())
        if not license_text:
            classifiers = [
                value.removeprefix("License :: ")
                for value in distribution.metadata.get_all("Classifier", [])
                if value.startswith("License :: ")
            ]
            license_text = "; ".join(classifiers)
        if not license_text:
            fallback = PYTHON_METADATA_FALLBACKS.get((_normalize_name(name), exact_pin or ""))
            if fallback:
                license_text = fallback["license"]
                license_source = fallback["source"]
                manual_review = fallback["manual_review"]
    except metadata.PackageNotFoundError:
        pass

    try:
        matches = not specifier or SpecifierSet(specifier).contains(Version(installed_version), prereleases=True)
    except Exception:
        matches = False
    ambiguous_tokens = ("see ", "dependency licenses", "lgpl", "license.txt", "unknown", "missing")
    manual_review = manual_review or not license_text or any(token in license_text.lower() for token in ambiguous_tokens)
    return {
        "installed_version": installed_version,
        "version_satisfies_declared_specifier": matches,
        "license": license_text or "MISSING",
        "license_source": license_source if license_text else None,
        "manual_review": manual_review or not matches,
    }


def read_installed_python_dependency_closure(direct_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk installed wheel METADATA offline, including explicitly requested extras."""
    environment = default_environment()
    direct_by_key = {_normalize_name(item["name"]): item for item in direct_items}
    records: dict[str, dict[str, Any]] = {}
    queue: list[tuple[str, set[str], str | None]] = [
        (item["name"], set(item["requested_extras"]), None) for item in direct_items
    ]
    processed_extras: dict[str, set[str] | None] = {}

    while queue:
        name, extras, parent = queue.pop(0)
        key = _normalize_name(name)
        record = records.setdefault(
            key,
            {
                "name": name,
                "direct": key in direct_by_key,
                "requested_extras": set(),
                "parents": set(),
                "declared_specifiers": set(),
                "roots": set(),
            },
        )
        record["requested_extras"].update(extras)
        if parent:
            record["parents"].add(parent)
        if key in direct_by_key:
            record["roots"].add(name)
        else:
            parent_key = _normalize_name(parent) if parent else None
            if parent_key and parent_key in records:
                record["roots"].update(records[parent_key]["roots"])

        previously = processed_extras.get(key)
        if previously is not None and extras.issubset(previously):
            continue
        processed_extras[key] = set(extras) | (previously or set())

        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        active_extras = {"", *processed_extras[key]}
        for raw_requirement in distribution.metadata.get_all("Requires-Dist", []):
            try:
                requirement = Requirement(raw_requirement)
            except Exception:
                continue
            if requirement.marker and not any(
                requirement.marker.evaluate({**environment, "extra": active_extra})
                for active_extra in active_extras
            ):
                continue
            child_key = _normalize_name(requirement.name)
            child = records.setdefault(
                child_key,
                {
                    "name": requirement.name,
                    "direct": child_key in direct_by_key,
                    "requested_extras": set(),
                    "parents": set(),
                    "declared_specifiers": set(),
                    "roots": set(record["roots"]),
                },
            )
            child["requested_extras"].update(requirement.extras)
            child["parents"].add(name)
            child["declared_specifiers"].add(str(requirement.specifier) or "*")
            child["roots"].update(record["roots"])
            queue.append((requirement.name, set(requirement.extras), name))

    result: list[dict[str, Any]] = []
    for key, record in records.items():
        direct_item = direct_by_key.get(key)
        specifier = direct_item["declared_specifier"] if direct_item else "; ".join(sorted(record["declared_specifiers"])) or "*"
        canonical_roots = {
            direct_by_key.get(_normalize_name(root), {}).get("name", root)
            for root in record["roots"]
        }
        try:
            installed_version = metadata.version(record["name"])
        except metadata.PackageNotFoundError:
            installed_version = None
        details = _python_license(record["name"], specifier, installed_version)
        if installed_version is None and direct_item is None:
            details = {
                "installed_version": None,
                "version_satisfies_declared_specifier": False,
                "license": "MISSING",
                "license_source": None,
                "manual_review": True,
            }
        result.append(
            {
                "name": record["name"],
                "dependency_class": "direct" if record["direct"] else "transitive",
                "declared_specifier": specifier,
                "installed_extras": sorted(record["requested_extras"]),
                "parents": sorted(record["parents"], key=str.lower),
                "roots": sorted(canonical_roots, key=str.lower),
                **details,
            }
        )
    return sorted(result, key=lambda item: (item["name"].lower(), item["dependency_class"]))


def build_inventory(root: Path) -> dict[str, Any]:
    python_items = []
    for item in read_python_requirements(root):
        try:
            installed_version = metadata.version(item["name"])
        except metadata.PackageNotFoundError:
            installed_version = None
        python_items.append({**item, **_python_license(item["name"], item["declared_specifier"], installed_version)})
    python_closure = read_installed_python_dependency_closure(python_items)

    npm_items = read_npm_lock_packages(root)
    return {
        "format": "infraspine-dependency-license-inventory",
        "scope": "declared Python requirements plus the complete checked-in frontend npm lockfile",
        "python": {
            "requirements_files": list(REQUIREMENT_FILES),
            "direct_requirement_count": len(python_items),
            "transitive_lockfile": None,
            "dependency_metadata_source": "installed Python distribution METADATA; no package index or network access",
            "installed_dependency_count": len(python_closure),
            "installed_dependency_license_metadata_present": sum(item["license"] != "MISSING" for item in python_closure),
            "installed_dependency_manual_review_count": sum(item["manual_review"] for item in python_closure),
            "items": python_items,
            "installed_dependency_closure": python_closure,
        },
        "frontend": {
            "package_lock": "frontend/package-lock.json",
            "lockfile_version": json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8")).get("lockfileVersion"),
            "locked_package_count": len(npm_items),
            "items_with_license_metadata": sum(item["license_metadata_present"] for item in npm_items),
            "items": npm_items,
        },
        "limitations": [
            "Python requirements declare direct packages but there is no checked-in transitive Python lock; this is not a complete Python SBOM.",
            "Python license values come from the local installed distribution metadata or the exact-version PyPI fallback listed per item; the local environment may differ from customer installations.",
            "Compound, non-SPDX, dependency-licensed and ambiguous labels require human legal review; this report is inventory data, not legal advice.",
            "The installer, PostgreSQL/Qdrant server binaries, OS packages, model weights, and embedded font/media assets are outside these two package manifests.",
        ],
    }


def render_markdown(inventory: dict[str, Any]) -> str:
    lines = [
        "# Dependency and license inventory",
        "",
        "Generated from the repository's declared Python requirements and the frontend npm lockfile by `scripts/build_dependency_license_inventory.py`.",
        "This is an inventory aid, not a legal opinion or a complete SBOM.",
        "",
        f"- Python direct requirements: **{inventory['python']['direct_requirement_count']}**; transitive Python lock: **not present**.",
            f"- Offline installed Python dependency closure: **{inventory['python']['installed_dependency_count']}** packages; **{inventory['python']['installed_dependency_license_metadata_present']}** with a license label.",
        f"- Frontend lock packages: **{inventory['frontend']['locked_package_count']}**; with license metadata: **{inventory['frontend']['items_with_license_metadata']}**.",
        "- Unpinned Python requirements and locally unavailable packages are marked in the JSON inventory.",
        "- Compound, non-SPDX, dependency-licensed, and ambiguous labels require legal review.",
        "",
        "## Python requirements",
        "",
        "| Package | Declared version | Local version | License metadata | Manual review | Source |",
        "|---|---:|---:|---|---:|---|",
    ]
    for item in inventory["python"]["items"]:
        source = item.get("license_source") or "not found"
        lines.append(
            f"| `{item['name']}` | `{item['declared_specifier']}` | `{item['installed_version'] or 'not installed'}` | {item['license']} | {'yes' if item['manual_review'] else 'no'} | {source} |"
        )
    lines.extend(
        [
            "",
            "## Installed Python dependency closure (offline metadata)",
            "",
            "This graph is derived from installed `Requires-Dist` metadata and the extras requested in the requirement files. It is not a reproducible lock; packages absent from this environment are listed as missing in JSON.",
            "",
            "| Package | Class | Local version | License metadata | Manual review | Reached from |",
            "|---|---|---:|---|---:|---|",
        ]
    )
    for item in inventory["python"]["installed_dependency_closure"]:
        lines.append(
            f"| `{item['name']}` | {item['dependency_class']} | `{item['installed_version'] or 'not installed'}` | {item['license']} | {'yes' if item['manual_review'] else 'no'} | {', '.join(item['roots'])} |"
        )
    lines.extend(
        [
            "",
            "## Frontend lockfile packages",
            "",
            "| Package | Version | Lockfile license | Class | Optional |",
            "|---|---:|---|---|---:|",
        ]
    )
    for item in inventory["frontend"]["items"]:
        lines.append(
            f"| `{item['name']}` | `{item['version'] or 'unknown'}` | {item['license']} | {item['dependency_class']} | {'yes' if item['optional'] else 'no'} |"
        )
    lines.extend(["", "## Scope limitations", ""])
    lines.extend(f"- {limitation}" for limitation in inventory["limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    inventory = build_inventory(root)
    (root / "docs/dependency-license-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "docs/dependency-license-inventory.md").write_text(render_markdown(inventory), encoding="utf-8")
    python_review = sum(item["manual_review"] for item in inventory["python"]["items"])
    python_unresolved = sum(item["license"] == "MISSING" for item in inventory["python"]["items"])
    npm_unresolved = sum(not item["license_metadata_present"] for item in inventory["frontend"]["items"])
    print(json.dumps({
        "python_direct": inventory["python"]["direct_requirement_count"],
        "python_missing_license": python_unresolved,
        "python_manual_review": python_review,
        "python_installed_dependency_count": inventory["python"]["installed_dependency_count"],
        "python_installed_dependency_manual_review": inventory["python"]["installed_dependency_manual_review_count"],
        "python_installed_dependency_missing_license": sum(item["license"] == "MISSING" for item in inventory["python"]["installed_dependency_closure"]),
        "python_transitive_lock": "missing",
        "frontend_locked": inventory["frontend"]["locked_package_count"],
        "frontend_missing_license": npm_unresolved,
        "markdown": "docs/dependency-license-inventory.md",
        "json": "docs/dependency-license-inventory.json",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
