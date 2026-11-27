"""Stable, content-addressed source identity. No .env, data, logs or ignored artifacts."""
import hashlib
import subprocess
from .catalog import ROOT, SUITES
from .domain import digest

def source_identity():
    files = []
    for folder in ("app", "tests", "frontend/src", "frontend/test", "migrations", "evals", "scripts", ".github/workflows"):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file() or path.is_symlink() or any(p in {"__pycache__", "reports", "node_modules"} for p in path.parts):
                continue
            if path.suffix in {".py", ".json", ".ts", ".tsx", ".mjs", ".yaml", ".yml", ".css"}:
                files.append((path.relative_to(ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    for name in ("requirements.txt", "requirements.lock", "pytest.ini", "frontend/package-lock.json", "frontend/package.json"):
        path = ROOT / name
        if path.exists():
            files.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=5, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        revision = "unavailable"
    return {"source_digest": digest(sorted(files)), "revision": revision, "manifest_sha256": digest(SUITES)}
