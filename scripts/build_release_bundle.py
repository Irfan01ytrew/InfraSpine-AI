"""Build a source release candidate from the current worktree without including local runtime data."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

_EXCLUDED_PARTS = {
    ".git", ".backups", ".local-db-backups", ".pytest_cache", ".verification", "__pycache__", "artifacts",
    "data", "dist", "imports", "logs", "node_modules", "onprem-data", "onprem-logs",
    "onprem-backups", "onprem-postgres", "onprem-qdrant", "venv",
}
_EXCLUDED_FILES = {
    "docs/completion-checkpoint.md", "docs/completion-matrix.md", "docs/release-readiness.md",
}
_EXCLUDED_SUFFIXES = {".bak", ".db", ".dump", ".log", ".sqlite", ".sqlite3", ".vhdx"}
_ENV_TEMPLATES = {".env.example", ".env.template", ".env.onprem.example"}


def _excluded(path: PurePosixPath) -> bool:
    if path.as_posix() in _EXCLUDED_FILES:
        return True
    lowered = tuple(part.lower() for part in path.parts)
    if any(
        part in _EXCLUDED_PARTS
        or part.startswith((".venv", ".verification-", ".verification_"))
        for part in lowered[:-1]
    ):
        return True
    name = path.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATES):
        return True
    return Path(name).suffix in _EXCLUDED_SUFFIXES


def _git_files(root: Path) -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    paths = [PurePosixPath(item) for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item]
    return sorted(path for path in paths if not _excluded(path))


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def _dirty(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return bool(result.stdout.strip())


def build_bundle(root: Path, destination: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Release bundle destination already exists; choose a new path.")
    try:
        relative_destination = destination.relative_to(root)
    except ValueError:
        raise ValueError("Release bundle must be written inside the repository artifacts directory.") from None
    if not relative_destination.parts or relative_destination.parts[0].lower() != "artifacts":
        raise ValueError("Release bundle must be written inside the repository artifacts directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved_parent = destination.parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except ValueError:
        raise ValueError("Release bundle directory cannot resolve outside the repository.") from None
    destination = resolved_parent / destination.name
    paths = _git_files(root)
    records: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    for relative in paths:
        source = root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            continue
        data = source.read_bytes()
        records.append({"path": relative.as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        payloads.append((relative.as_posix(), data))
    manifest = {
        "format": "infraspine-source-release-candidate",
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": _head(root),
        "dirty_worktree": _dirty(root),
        "files": records,
    }
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, data in payloads:
            archive.writestr(f"infraspine-ai/{relative}", data)
        archive.writestr(
            "infraspine-ai/RELEASE-MANIFEST.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    return {
        "path": str(destination),
        "git_head": manifest["git_head"],
        "files": len(records),
        "bytes": destination.stat().st_size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="new bundle path under <repo>/artifacts/")
    args = parser.parse_args()
    result = build_bundle(args.root, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
