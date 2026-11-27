"""Source release candidates include safe deployment templates but never runtime environment files."""

from pathlib import PurePosixPath
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from scripts.build_release_bundle import _excluded
from scripts.verify_release_bundle import verify_bundle


def test_onprem_environment_template_is_packaged() -> None:
    assert not _excluded(PurePosixPath(".env.onprem.example"))
    assert not _excluded(PurePosixPath(".env.example"))


def test_runtime_environment_files_are_excluded() -> None:
    assert _excluded(PurePosixPath(".env"))
    assert _excluded(PurePosixPath(".env.onprem"))


def test_local_verification_folders_are_excluded_from_release_candidates() -> None:
    assert _excluded(PurePosixPath(".verification/evidence/browser.json"))
    assert _excluded(PurePosixPath(".verification-local-review/credentials.json"))
    assert _excluded(PurePosixPath(".verification_local_review/logs/api.log"))


def _write_bundle(path: Path, files: dict[str, bytes]) -> None:
    records = [
        {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in files.items()
    ]
    manifest = {"format": "infraspine-source-release-candidate", "files": records}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(f"infraspine-ai/{name}", payload)
        archive.writestr(
            "infraspine-ai/RELEASE-MANIFEST.json",
            json.dumps(manifest).encode("utf-8"),
        )


def test_release_bundle_verifier_checks_integrity_and_scans_payload() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / "candidate.zip"
        _write_bundle(bundle, {"README.md": b"Synthetic release fixture."})
        verified = verify_bundle(bundle)

    assert verified["payload_files"] == 1
    assert verified["zip_crc"] == "PASS"
    assert verified["manifest_hashes"] == "PASS"
    assert verified["secret_patterns"] == 0


def test_release_bundle_verifier_rejects_runtime_env_and_secret_pattern() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime_bundle = root / "runtime.zip"
        _write_bundle(runtime_bundle, {".env.production": b"never included"})
        try:
            verify_bundle(runtime_bundle)
        except ValueError as error:
            assert "runtime environment" in str(error)
        else:
            raise AssertionError("runtime environment file was accepted")

        secret_bundle = root / "secret.zip"
        sample = ("TOKEN=" + "sk-" + "x" * 30).encode()
        _write_bundle(secret_bundle, {"app/config.py": sample})
        try:
            verify_bundle(secret_bundle)
        except ValueError as error:
            assert "credential-shaped" in str(error)
            assert "sk-" not in str(error)
        else:
            raise AssertionError("credential-shaped value was accepted")
