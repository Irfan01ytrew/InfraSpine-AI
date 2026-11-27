"""Synthetic checks for the safe-output release secret scanner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.scan_release_secrets import scan


class ReleaseSecretScanTests(unittest.TestCase):
    def test_detects_token_shapes_without_returning_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text(
                'provider = "' + "sk-" + "x" * 30 + '"\n'
                'url = "https://qa:' + "p" * 24 + '@example.test"\n',
                encoding="utf-8",
            )
            _, findings = scan(root, [source])

        self.assertEqual([(item.line, item.rule) for item in findings], [(1, "openai-token"), (2, "url-basic-auth")])
        self.assertNotIn("sk-", repr(findings))
        self.assertNotIn("https://", repr(findings))

    def test_runtime_environment_files_are_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".env.qa"
            runtime.write_text('KEY="sk-' + "x" * 30 + '"', encoding="utf-8")
            source = root / ".env.example"
            source.write_text('KEY="replace-this-example"', encoding="utf-8")
            scanned, findings = scan(root, [runtime, source])

        self.assertEqual(scanned, 1)
        self.assertEqual(findings, [])

    def test_onprem_environment_template_is_included_and_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / ".env.onprem.example"
            template.write_text('KEY="' + "sk-" + "x" * 30 + '"', encoding="utf-8")
            scanned, findings = scan(root, [template])

        self.assertEqual(scanned, 1)
        self.assertEqual([(item.line, item.rule) for item in findings], [(1, "openai-token")])
        self.assertNotIn("sk-", repr(findings))

    def test_directory_scan_skips_binary_and_environment_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload.bin").write_bytes(b"\0sk-" + b"x" * 30)
            env_dir = root / ".venv-test"
            env_dir.mkdir()
            (env_dir / "sample.txt").write_text('sk-' + "x" * 30, encoding="utf-8")
            source = root / "README.md"
            source.write_text("No credential here.", encoding="utf-8")
            scanned, findings = scan(root, [root])

        self.assertEqual(scanned, 2)
        self.assertEqual(findings, [])

    def test_local_verification_fixtures_are_outside_release_scan_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verification = root / ".verification-local-review-test"
            verification.mkdir()
            fixture = verification / "credentials.json"
            fixture.write_text('API_KEY="' + "sk-" + "x" * 30 + '"', encoding="utf-8")
            source = root / "README.md"
            source.write_text("Public release note.", encoding="utf-8")
            scanned, findings = scan(root)

        self.assertEqual(scanned, 1)
        self.assertEqual(findings, [])

    def test_explicit_release_bundle_scan_does_not_hide_verification_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verification = root / ".verification-local-review-test"
            verification.mkdir()
            fixture = verification / "credentials.json"
            fixture.write_text('API_KEY="' + "sk-" + "x" * 30 + '"', encoding="utf-8")
            scanned, findings = scan(root, [root])

        self.assertEqual(scanned, 1)
        self.assertEqual([(item.rule) for item in findings], ["openai-token"])
        self.assertNotIn("sk-", repr(findings))


if __name__ == "__main__":
    unittest.main()
