"""Step AO slice 4: secrets audit — no secret in code, logs, receipts or health output.

The audit scans text (a log line, a receipt, a health payload) and files
for secret patterns. A finding names the pattern and the location; the
audit never prints the secret itself — a report that leaks the secret
it found would be the leak.

Placeholder values (``changeme``, ``example``, ``your-...``, ``...``)
are not findings: example files document the shape of a secret, they
do not hold one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: (pattern name, compiled pattern). Each pattern matches the *shape* of a
#: secret being written down, never a specific value.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "database_url_with_password",
        re.compile(r"(postgres|postgresql|mysql)://[^:/\s@]+:[^@\s]+@[^\s]+", re.IGNORECASE),
    ),
    (
        "pgpassword_assignment",
        re.compile(r"PGPASSWORD\s*=\s*\S+", re.IGNORECASE),
    ),
    (
        "password_assignment",
        # Quoted values only: an unquoted ``password=some_variable`` is a
        # variable reference, not a written-down secret.
        re.compile(r"""(?i)\bpassword\s*[:=]\s*['\"][^'\"]{4,}['\"]"""),
    ),
    (
        "api_key_assignment",
        re.compile(r"""(?i)\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]"""),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    ),
    (
        "aws_access_key_id",
        re.compile(r"AKIA[0-9A-Z]{16}"),
    ),
    (
        "github_pat",
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    ),
    (
        "master_key_assignment",
        re.compile(r"(?i)MASTER_ENCRYPTION_KEY\s*[:=]\s*['\"]?[A-Za-z0-9+/=]{16,}"),
    ),
)

_PLACEHOLDERS = ("changeme", "change-me", "example", "your-", "placeholder", "...", "xxx")


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDERS)


def scan_text_for_secrets(text: str, *, source: str = "") -> list[dict]:
    """Findings in one piece of text. Each finding names the pattern, never the secret."""
    findings: list[dict] = []
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # Placeholders are documentation, not secrets — but the check
            # happens on the match locally; the matched text is never stored.
            if _is_placeholder(match.group(0)):
                continue
            findings.append(
                {
                    "pattern": name,
                    "source": source,
                    # No matched text is ever recorded: a report that
                    # quotes the secret it found would be the leak.
                    "context": "",
                }
            )
    return findings


def audit_paths(paths: list[Path]) -> list[dict]:
    """Scan files for secrets. Skips example files and unreadable/binary files silently."""
    findings: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix == ".example" or ".example." in path.name:
            continue
        try:
            content = path.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for finding in scan_text_for_secrets(line, source=str(path)):
                findings.append({**finding, "line": line_number})
    return findings


def audit_payload(payload: object) -> list[dict]:
    """Scan a JSON-serializable payload (a health report, a receipt) for secrets.

    Connection strings must never appear in health output, receipts or
    logs — this is the check slice 5's endpoint tests call.
    """
    try:
        rendered = json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return [{"pattern": "unserializable_payload", "source": "", "context": ""}]
    return scan_text_for_secrets(rendered, source="payload")
