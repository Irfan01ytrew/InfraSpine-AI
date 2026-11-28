#!/usr/bin/env python3
"""Build InfraSpine-after-stepE.zip from a clean merged `main` checkout.

Written by Agent 10 (docs-packaging). WRITTEN, NOT RUN.
Run only after the merge gate in assessment/REVIEW-CHECKLIST.md passes,
from a clean checkout of the merged `main`, and review the output before
sending the zip to the owner. Do NOT live-push from this zip.

Usage:
    python3 assessment/package_after_step_e.py \
        --repo . \
        --out ~/workspace/infraspine/InfraSpine-after-stepE.zip

What it includes: the repo + full git history (.git) + all docs, including
assessment/CHANGES-step-E.md (final, no [TO FILL] left), REVIEW-CHECKLIST.md,
PACKAGING-INFRA-SPINE-AFTER-STEPE.md, and HANDOVER-PROMPT.md. It also writes
a generated MANIFEST.txt (merged commit, branch, build time, file count;
test counts and gaps live in CHANGES-step-E.md).

What it excludes (never in the zip):
    .venv/, frontend/node_modules/, frontend/dist/, .env (except .example),
    uploads/, data/, logs/, *.log, __pycache__/, *.pyc, .pytest_cache/,
    any *_test database dump, the live folder, the old InfraSpine-source-20261125 zip.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - very old Python
    ZoneInfo = None

EXCLUDE_DIRS = {
    ".venv", "node_modules", "dist", "uploads", "data", "logs",
    "__pycache__", ".pytest_cache", ".git",  # .git is added separately, as a whole
}
# Note: "dist" is excluded only under frontend/ (see _is_excluded).
EXCLUDE_SUFFIXES = (".log", ".pyc", ".pyo")
EXCLUDE_FILES = {
    ".env", ".env.local", ".env.production",
    "InfraSpine-source-20261125.zip",
}
EXCLUDE_PREFIXES = (".env.",)  # except .env.example / .env.docker.example


def _git(repo: str, *args: str) -> str:
    """Best-effort git output ('' when git is missing or the command fails)."""
    try:
        out = subprocess.run(["git", "-C", repo, *args],
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _manifest_text(repo: str, files_written: int) -> str:
    """MANIFEST.txt content: only what this script can verify by itself.

    Test counts, the Alembic head, frontend results and the 5 gaps with
    their commit ids live in assessment/CHANGES-step-E.md — the manifest
    points at that file instead of re-stating (and possibly mis-stating)
    them.
    """
    commit = _git(repo, "rev-parse", "HEAD") or "unknown"
    branch = _git(repo, "branch", "--show-current") or "unknown"
    if ZoneInfo is not None:
        now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d %H:%M %Z")
    else:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([
        "InfraSpine-after-stepE — MANIFEST",
        "",
        f"Built: {now}",
        f"Repo: {repo}",
        f"Merged commit: {commit}",
        f"Branch: {branch}",
        f"Files in zip: {files_written} (including this manifest)",
        "",
        "Test counts, Alembic head, frontend test/build results, and the",
        "5 gaps with their commit ids: see assessment/CHANGES-step-E.md",
        "(final, no [PENDING] markers).",
        "",
    ])


def _is_excluded(rel_path: str) -> bool:
    parts = rel_path.replace(os.sep, "/").split("/")
    name = parts[-1]
    if name in EXCLUDE_FILES:
        return True
    if name in (".env.example", ".env.docker.example"):
        return False
    if name.startswith(".env."):
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    for part in parts[:-1]:
        if part in EXCLUDE_DIRS and part != "dist":
            return True
    # frontend/dist only; a top-level dist (if any) is also excluded.
    if "dist" in parts[:-1]:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="Clean checkout of merged main")
    ap.add_argument("--out", required=True, help="Output zip path")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    out = os.path.abspath(args.out)

    # Safety: refuse to run with uncommitted changes is the caller's job
    # (check `git status` before running). Here we only refuse an empty repo.
    git_path = os.path.join(repo, ".git")
    if os.path.isfile(git_path):
        # A git worktree: .git is a pointer file, not the history itself,
        # so the zip would carry a dangling pointer instead of .git.
        print(f"ERROR: {repo} is a git worktree (.git is a file, not a "
              "directory) — run from a normal clone/checkout of the merged "
              "main, not a worktree.", file=sys.stderr)
        return 2
    if not os.path.isdir(git_path):
        print(f"ERROR: {repo} has no .git — run from a full repo checkout.", file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(repo, "assessment", "CHANGES-step-E.md")):
        print("ERROR: assessment/CHANGES-step-E.md is missing.", file=sys.stderr)
        return 2
    with open(os.path.join(repo, "assessment", "CHANGES-step-E.md"), encoding="utf-8") as f:
        changes = f.read()
        if "[TO FILL]" in changes:
            print("ERROR: CHANGES-step-E.md still has [TO FILL] placeholders.", file=sys.stderr)
            return 2
        if "[PENDING" in changes:
            print("ERROR: CHANGES-step-E.md still has [PENDING ...] markers "
                  "(Agent 14, docs-final) — fill them from the Agents 8/11 results first.",
                  file=sys.stderr)
            return 2

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    written = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo):
            # Do not descend into excluded dirs (except .git, handled below).
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS or d == ".git"]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, repo)
                if rel.startswith(".git" + os.sep):
                    # Include full git history, as the handover zip did.
                    zf.write(full, rel)
                    written += 1
                    continue
                if _is_excluded(rel):
                    continue
                if os.path.abspath(full) == out:
                    continue
                zf.write(full, rel)
                written += 1

        # MANIFEST.txt last: it records the commit/branch/build time and
        # the file count (itself included), and points at CHANGES-step-E.md
        # for the test counts and gap list (see PACKAGING-...md).
        zf.writestr("MANIFEST.txt", _manifest_text(repo, written + 1))
        written += 1

    sha = hashlib.sha256()
    with open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    size = os.path.getsize(out)
    print(f"Wrote {out}")
    print(f"Files: {written}")
    print(f"Bytes: {size}")
    print(f"SHA-256: {sha.hexdigest()}")
    print("Verify with: unzip -l <zip>  (check for .env / .venv / node_modules / uploads / data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
