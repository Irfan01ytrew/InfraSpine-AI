"""Unit tests for Step AO slice 4: secrets audit (no database)."""

from pathlib import Path

from app.ops.secrets_audit import audit_paths, audit_payload, scan_text_for_secrets


def test_scan_finds_a_database_url_with_a_password():
    findings = scan_text_for_secrets(
        "connecting to postgresql://app:s3cret-value@127.0.0.1:5432/infraspine"
    )
    assert [finding["pattern"] for finding in findings] == ["database_url_with_password"]
    # The finding names the pattern, never the secret itself.
    assert not any("s3cret-value" in str(finding) for finding in findings)


def test_scan_finds_quoted_password_and_key_assignments():
    findings = scan_text_for_secrets('password = "hunter2-hunter2"')
    assert any(finding["pattern"] == "password_assignment" for finding in findings)
    findings = scan_text_for_secrets('api_key = "abcdef0123456789"')
    assert any(finding["pattern"] == "api_key_assignment" for finding in findings)
    findings = scan_text_for_secrets("Authorization: Bearer abcdef0123456789")
    assert any(finding["pattern"] == "bearer_token" for finding in findings)


def test_scan_ignores_variable_references_and_placeholders():
    assert scan_text_for_secrets("password = some_variable") == []
    assert scan_text_for_secrets('password = "changeme"') == []
    assert scan_text_for_secrets('api_key = "example-key-0123456789"') == []
    assert scan_text_for_secrets("password: str") == []
    assert scan_text_for_secrets("no secrets here, just a normal log line") == []


def test_scan_finds_aws_and_github_and_master_key_shapes():
    # The canonical AWS example key (AKIA...EXAMPLE) is a placeholder and
    # is deliberately NOT flagged; a realistic key shape is.
    assert scan_text_for_secrets("AKIAIOSFODNN7EXAMPLE") == []
    assert scan_text_for_secrets("AKIAIOSFODNN7XKCD123") != []
    assert scan_text_for_secrets("ghp_" + "a" * 36) != []
    assert scan_text_for_secrets("MASTER_ENCRYPTION_KEY=" + "A" * 44) != []


def test_audit_paths_skips_examples_and_finds_a_real_leak(tmp_path: Path):
    clean = tmp_path / "clean.py"
    clean.write_text("DATABASE_URL = os.environ['DATABASE_URL']\n")
    leaked = tmp_path / "leaked.py"
    leaked.write_text('BACKUP_URL = "postgresql://app:s3cret-value@db:5432/app"\n')
    example = tmp_path / "config.env.example"
    example.write_text('DATABASE_URL="postgresql://user:changeme@localhost/db"\n')
    findings = audit_paths([clean, leaked, example])
    assert len(findings) == 1
    assert findings[0]["pattern"] == "database_url_with_password"
    assert findings[0]["line"] == 1


def test_audit_payload_flags_a_leaked_connection_string():
    assert audit_payload({"database": "ok", "url": "postgresql://app:s3cret-value@db/app"}) != []
    assert audit_payload({"status": "ready", "database": "ok", "connections": {"total": 3}}) == []


def test_audit_of_the_new_ao_modules_finds_nothing():
    repo = Path(__file__).resolve().parents[1]
    findings = audit_paths(
        [
            repo / "app" / "ops" / "backup_verify.py",
            repo / "app" / "ops" / "db_health.py",
            repo / "app" / "ops" / "db_performance.py",
            repo / "app" / "ops" / "db_roles.py",
            repo / "app" / "ops" / "secrets_audit.py",
            repo / "app" / "ops" / "backup_schedule.py",
        ]
    )
    assert findings == []
