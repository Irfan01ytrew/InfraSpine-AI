"""Find common high-confidence credential formats without printing matched values.

This is a release hygiene check, not a complete secret-management solution. Runtime
environment files are deliberately excluded and must never be passed to this scanner.
For an unpacked release bundle, pass its directory with ``--path``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_TEXT_BYTES = 8 * 1024 * 1024
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "venv", "tests", "evals"}
_ENV_TEMPLATES = {".env.example", ".env.template", ".env.onprem.example"}


def _is_local_verification_dir(name: str) -> bool:
    lowered = name.lower()
    return lowered == ".verification" or lowered.startswith((".verification-", ".verification_"))


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("url-basic-auth", re.compile(r"https?://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def _excluded(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    if any(part in _SKIP_DIRS or part.startswith(".venv") for part in parts[:-1]):
        return True
    name = path.name.lower()
    return name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATES)


def _git_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return [root / item for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item]


def _walk(root: Path, *, include_local_verification: bool = True) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs
            if d.lower() not in _SKIP_DIRS
            and not d.lower().startswith(".venv")
            and (include_local_verification or not _is_local_verification_dir(d))
        ]
        for name in files:
            yield Path(current) / name


def _inside_local_verification(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(_is_local_verification_dir(part) for part in relative.parts[:-1])


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def scan_files(paths: Iterable[Path], *, display_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
            if resolved in seen or _excluded(path):
                continue
            seen.add(resolved)
            if not resolved.is_file() or resolved.stat().st_size > MAX_TEXT_BYTES:
                continue
            data = resolved.read_bytes()
        except (OSError, ValueError):
            continue
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in _RULES:
                if pattern.search(line):
                    findings.append(Finding(_display(path, display_root), line_number, rule))
    return findings


def scan(root: Path, explicit_paths: list[Path] | None = None) -> tuple[int, list[Finding]]:
    root = root.resolve()
    if explicit_paths:
        paths: list[Path] = []
        for raw in explicit_paths:
            target = raw if raw.is_absolute() else root / raw
            if target.is_dir():
                paths.extend(_walk(target))
            else:
                paths.append(target)
    else:
        try:
            paths = [path for path in _git_files(root) if not _inside_local_verification(path, root)]
        except (OSError, subprocess.CalledProcessError):
            paths = list(_walk(root, include_local_verification=False))
    findings = scan_files(paths, display_root=root)
    return len(set(p.resolve() for p in paths if p.exists() and not _excluded(p))), findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--path", type=Path, action="append", default=None,
        help="scan this file or directory (repeatable); otherwise scan Git-tracked and non-ignored worktree files",
    )
    args = parser.parse_args()
    scanned, findings = scan(args.root, args.path)
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}")
        print(f"Secret scan failed: {len(findings)} finding(s); matched content was not printed.", file=sys.stderr)
        return 1
    print(f"Secret scan passed: {scanned} files scanned; matched content was not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
