from __future__ import annotations

import json
from pathlib import Path

from scripts.build_dependency_license_inventory import read_npm_lock_packages, read_python_requirements


def test_python_requirements_resolve_nested_includes_and_keep_optional_files(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==1.2.3\n", encoding="utf-8")
    (tmp_path / "requirements-media.txt").write_text("faster-whisper==2.3.4\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest==8.0.0\n", encoding="utf-8")
    (tmp_path / "requirements-reporting.txt").write_text("reportlab==5.0.0\n", encoding="utf-8")
    (tmp_path / "requirements-document-tables.txt").write_text("pdfplumber==0.11.0\n", encoding="utf-8")
    (tmp_path / "requirements-browser.txt").write_text("playwright==1.0.0\n", encoding="utf-8")

    items = read_python_requirements(tmp_path)

    assert {item["name"] for item in items} == {"fastapi", "faster-whisper", "pytest", "reportlab", "pdfplumber", "playwright"}
    fastapi = next(item for item in items if item["name"] == "fastapi")
    assert fastapi["declared_specifier"] == "==1.2.3"
    assert set(fastapi["requirement_files"]) == {"requirements.txt", "requirements-dev.txt"}


def test_npm_lock_inventory_keeps_runtime_dev_and_transitive_license_fields(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^19.0.0"},
        "devDependencies": {"vite": "^8.0.0"},
    }), encoding="utf-8")
    (frontend / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {},
            "node_modules/react": {"version": "19.0.0", "license": "MIT"},
            "node_modules/vite": {"version": "8.0.0", "license": "MIT", "dev": True},
            "node_modules/rollup": {"version": "4.0.0", "license": "MIT", "dev": True, "optional": True},
        },
    }), encoding="utf-8")

    items = {item["name"]: item for item in read_npm_lock_packages(tmp_path)}

    assert items["react"]["dependency_class"] == "runtime"
    assert items["vite"]["dependency_class"] == "development"
    assert items["rollup"]["dependency_class"] == "transitive"
    assert items["rollup"]["optional"] is True
    assert all(item["license_metadata_present"] for item in items.values())
