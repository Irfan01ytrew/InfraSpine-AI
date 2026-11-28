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
PACKAGING-INFRA-SPINE-AFTER-STEPE.md, and HANDOVER-PROMPT.md.

What it excludes (never in the zip):
    .venv/, frontend/node_modules/, frontend/dist/, .env (except .example),
    uploads/, data/, logs/, *.log, __pycache__/, *.pyc, .pytest_cache/,
    any *_test database dump, the live folder, the old InfraSpine-source-20261125 zip.
"""

import argparse
import hashlib
import os
import sys
import zipfile

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
    if not os.path.isdir(os.path.join(repo, ".git")):
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
