"""Verify a source bundle's archive integrity, manifest hashes and credential scan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

try:
    from scan_release_secrets import MAX_TEXT_BYTES, _ENV_TEMPLATES, _RULES
except ModuleNotFoundError:
    from scripts.scan_release_secrets import MAX_TEXT_BYTES, _ENV_TEMPLATES, _RULES

MANIFEST_PATH = "infraspine-ai/RELEASE-MANIFEST.json"


def _runtime_environment_file(path: PurePosixPath) -> bool:
    name = path.name.lower()
    return name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATES)


def verify_bundle(bundle_path: Path) -> dict[str, object]:
    bundle_path = bundle_path.resolve(strict=True)
    digest = hashlib.sha256()
    with bundle_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    with zipfile.ZipFile(bundle_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("The release archive failed its ZIP CRC check.")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("The release archive contains duplicate member names.")
        if names.count(MANIFEST_PATH) != 1:
            raise ValueError("The release archive is missing its single root manifest.")
        try:
            manifest = json.loads(archive.read(MANIFEST_PATH))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("The release manifest is not valid UTF-8 JSON.") from None
        if manifest.get("format") != "infraspine-source-release-candidate" or not isinstance(manifest.get("files"), list):
            raise ValueError("The release manifest format is unsupported.")

        records: dict[str, dict[str, object]] = {}
        for record in manifest["files"]:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise ValueError("The release manifest has an invalid file record.")
            relative = PurePosixPath(record["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in record["path"]:
                raise ValueError("The release manifest contains an unsafe file path.")
            name = f"infraspine-ai/{relative.as_posix()}"
            if name in records or name not in names:
                raise ValueError("The release manifest has a duplicate or missing file entry.")
            if _runtime_environment_file(relative):
                raise ValueError("A runtime environment file is included in the release archive.")
            records[name] = record

        payload_names = set(names) - {MANIFEST_PATH}
        if payload_names != set(records):
            raise ValueError("The archive payload and manifest file list do not match.")

        findings: list[tuple[str, int, str]] = []
        binary_files = 0
        oversized_files = 0
        for name, record in records.items():
            info = archive.getinfo(name)
            if info.file_size != record.get("size"):
                raise ValueError(f"Manifest size mismatch: {name}")
            file_hash = hashlib.sha256()
            scan_data = bytearray() if info.file_size <= MAX_TEXT_BYTES else None
            has_nul = False
            with archive.open(info) as member:
                while chunk := member.read(1024 * 1024):
                    file_hash.update(chunk)
                    if scan_data is not None:
                        scan_data.extend(chunk)
                        has_nul = has_nul or b"\0" in chunk
            if file_hash.hexdigest() != record.get("sha256"):
                raise ValueError(f"Manifest SHA-256 mismatch: {name}")
            if scan_data is None:
                oversized_files += 1
                continue
            if has_nul:
                binary_files += 1
                continue
            text = scan_data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for rule, pattern in _RULES:
                    if pattern.search(line):
                        findings.append((name.removeprefix("infraspine-ai/"), line_number, rule))

    if findings:
        for relative, line, rule in findings:
            print(f"SECRET_PATTERN {relative}:{line}: {rule}", file=sys.stderr)
        raise ValueError(f"The release scan found {len(findings)} credential-shaped pattern(s); matched content is not shown.")

    return {
        "archive": str(bundle_path),
        "archive_bytes": bundle_path.stat().st_size,
        "archive_sha256": digest.hexdigest(),
        "payload_files": len(records),
        "manifest_hashes": "PASS",
        "zip_crc": "PASS",
        "runtime_env_files": 0,
        "secret_patterns": 0,
        "binary_files_not_text_scanned": binary_files,
        "oversized_files_not_text_scanned": oversized_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True, help="source bundle ZIP to verify")
    args = parser.parse_args()
    try:
        result = verify_bundle(args.path)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
