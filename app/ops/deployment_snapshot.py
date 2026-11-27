"""Integrity-checked file snapshots to pair with a verified PostgreSQL dump."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.ops.postgres_backup import verify_backup

MANIFEST_NAME = "snapshot.json"
SNAPSHOT_VERSION = 1
_ROOTS = {"application-data", "qdrant-data"}


def create_deployment_snapshot(
    destination: Path,
    database_backup: Path,
    application_data: Path,
    qdrant_data: Path | None = None,
) -> dict[str, Any]:
    """Copy verified deployment files and a pg_dump into a new, non-overwriting bundle.

    Call only while API/worker writes and (when configured) Qdrant writes are stopped.
    A failed invocation preserves its incomplete bundle for inspection; it never removes input data.
    """
    destination_input = destination.expanduser()
    if destination_input.is_symlink():
        raise ValueError("Snapshot destination cannot be a symbolic link.")
    destination = destination_input.resolve()
    if destination.exists():
        raise FileExistsError("Snapshot destination already exists; choose a new directory.")
    database_input = database_backup.expanduser()
    if database_input.is_symlink():
        raise ValueError("Database backup must be a regular file.")
    database_backup = database_input.resolve(strict=True)
    if not database_backup.is_file():
        raise ValueError("Database backup must be a regular file.")
    if verify_backup(database_backup) < 1:
        raise ValueError("Database backup has no catalog entries.")

    app_source = application_data.expanduser()
    if app_source.is_symlink():
        raise ValueError("application-data must be a regular directory.")
    sources: dict[str, Path] = {"application-data": app_source.resolve(strict=True)}
    if qdrant_data is not None:
        qdrant_source = qdrant_data.expanduser()
        if qdrant_source.is_symlink():
            raise ValueError("qdrant-data must be a regular directory.")
        sources["qdrant-data"] = qdrant_source.resolve(strict=True)
    if len(sources) > 1:
        app_root, qdrant_root = sources["application-data"], sources["qdrant-data"]
        if app_root == qdrant_root or app_root.is_relative_to(qdrant_root) or qdrant_root.is_relative_to(app_root):
            raise ValueError("Application and Qdrant source directories must be separate.")
    for name, source in sources.items():
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"{name} must be a regular directory.")
        if destination == source or destination.is_relative_to(source):
            raise ValueError("Snapshot destination cannot be inside a source directory.")

    # This mkdir is the sole bundle creation point. Existing paths are rejected above and never reused.
    destination.mkdir(parents=True, exist_ok=False)
    file_records: dict[str, dict[str, Any]] = {}
    (destination / "database").mkdir()
    _copy_file(database_backup, destination / "database" / "database.dump", file_records, "database/database.dump")
    if verify_backup(destination / "database" / "database.dump") < 1:
        raise ValueError("Copied database backup has no catalog entries.")
    root_records: dict[str, list[str]] = {}
    for name, source in sources.items():
        target = destination / name
        target.mkdir()
        root_records[name] = _copy_tree(source, target, file_records, name)

    manifest = {
        "format": "infraspine-deployment-snapshot",
        "version": SNAPSHOT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "roots": root_records,
        "files": file_records,
    }
    manifest_path = destination / MANIFEST_NAME
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(destination), "files": len(file_records), "bytes": sum(item["size"] for item in file_records.values()), "verified": True}


def verify_deployment_snapshot(snapshot: Path) -> dict[str, Any]:
    snapshot_input = snapshot.expanduser()
    if snapshot_input.is_symlink():
        raise ValueError("Snapshot path must be a regular directory.")
    snapshot = snapshot_input.resolve(strict=True)
    if not snapshot.is_dir():
        raise ValueError("Snapshot path must be a regular directory.")
    manifest_path = snapshot / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Snapshot manifest is missing or is not a regular file.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("Snapshot manifest cannot be read.") from None
    _validate_manifest(manifest)

    expected = set(manifest["files"])
    actual: set[str] = set()
    for root, dirs, files in os.walk(snapshot, followlinks=False):
        root_path = Path(root)
        for name in dirs:
            if (root_path / name).is_symlink():
                raise ValueError("Snapshot contains a symbolic link.")
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise ValueError("Snapshot contains a symbolic link.")
            relative = path.relative_to(snapshot).as_posix()
            if relative != MANIFEST_NAME:
                actual.add(relative)
    if actual != expected:
        raise ValueError("Snapshot file inventory does not match its manifest.")

    total_bytes = 0
    for relative, record in manifest["files"].items():
        path = snapshot.joinpath(*PurePosixPath(relative).parts)
        size, digest = _measure(path)
        if size != record["size"] or digest != record["sha256"]:
            raise ValueError(f"Snapshot file failed integrity verification: {relative}")
        total_bytes += size
    if verify_backup(snapshot / "database" / "database.dump") < 1:
        raise ValueError("Snapshot database backup has no catalog entries.")
    return {"path": str(snapshot), "files": len(expected), "bytes": total_bytes, "verified": True}


def restore_snapshot_files(
    snapshot: Path,
    application_data_target: Path,
    qdrant_data_target: Path | None = None,
) -> dict[str, str]:
    """Restore file roots only to paths that do not exist; database restore remains a separate empty-DB step."""
    snapshot_input = snapshot.expanduser()
    if snapshot_input.is_symlink():
        raise ValueError("Snapshot path must be a regular directory.")
    snapshot = snapshot_input.resolve(strict=True)
    manifest = _load_verified_manifest(snapshot)
    roots = manifest["roots"]
    requested: dict[str, Path] = {"application-data": application_data_target.expanduser().resolve()}
    if "qdrant-data" in roots:
        if qdrant_data_target is None:
            raise ValueError("This snapshot contains Qdrant data; supply a new, empty destination path.")
        requested["qdrant-data"] = qdrant_data_target.expanduser().resolve()
    elif qdrant_data_target is not None:
        raise ValueError("A Qdrant destination was supplied, but the snapshot has no Qdrant data.")

    if set(requested) != set(roots):
        raise ValueError("Restore targets do not match the snapshot roots.")
    targets = list(requested.values())
    if len(targets) > 1 and (targets[0] == targets[1] or targets[0].is_relative_to(targets[1]) or targets[1].is_relative_to(targets[0])):
        raise ValueError("Restore targets must be separate directories.")
    for target in requested.values():
        if target.exists():
            raise FileExistsError("Restore target already exists; restore only to a new path.")
    for target in requested.values():
        if target == snapshot or target.is_relative_to(snapshot):
            raise ValueError("Restore target cannot be inside the snapshot directory.")

    staged: list[tuple[str, Path, Path]] = []
    try:
        for name, target in requested.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name(f".{target.name}.infraspine-restore-stage")
            if stage.exists():
                raise FileExistsError("Restore staging path already exists; preserve it and choose another target.")
            shutil.copytree(snapshot / name, stage, symlinks=False)
            staged.append((name, stage, target))
        _verify_restored_roots(manifest, staged)
        for _name, stage, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError("Restore target appeared during restore; preserve all files and retry elsewhere.")
            os.rename(stage, target)
    except BaseException:
        # Preserve staged files for manual inspection if any step fails.
        raise
    return {name: str(target) for name, target in requested.items()}


def _load_verified_manifest(snapshot: Path) -> dict[str, Any]:
    verify_deployment_snapshot(snapshot)
    return json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))


def _validate_manifest(value: Any) -> None:
    if not isinstance(value, dict) or value.get("format") != "infraspine-deployment-snapshot" or value.get("version") != SNAPSHOT_VERSION:
        raise ValueError("Unsupported deployment snapshot format.")
    roots, files = value.get("roots"), value.get("files")
    if not isinstance(roots, dict) or not set(roots) <= _ROOTS or "application-data" not in roots:
        raise ValueError("Snapshot root inventory is invalid.")
    if "qdrant-data" in roots and roots["qdrant-data"] is None:
        raise ValueError("Snapshot root inventory is invalid.")
    if not isinstance(files, dict) or "database/database.dump" not in files:
        raise ValueError("Snapshot file inventory is invalid.")
    for relative, record in files.items():
        path = PurePosixPath(relative)
        if (
            path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts
            or "\\" in relative or ":" in relative or "\x00" in relative or relative == MANIFEST_NAME
        ):
            raise ValueError("Snapshot contains an unsafe path.")
        if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0:
            raise ValueError("Snapshot file record is invalid.")
        digest = record.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Snapshot file digest is invalid.")
        if relative != "database/database.dump" and not any(relative.startswith(root + "/") for root in roots):
            raise ValueError("Snapshot file is outside its declared roots.")
    if any(not isinstance(paths, list) or any(not isinstance(item, str) for item in paths) for paths in roots.values()):
        raise ValueError("Snapshot root file lists are invalid.")
    if set(roots["application-data"]) != {path.removeprefix("application-data/") for path in files if path.startswith("application-data/")}:
        raise ValueError("Application data inventory is inconsistent.")
    if "qdrant-data" in roots and set(roots["qdrant-data"]) != {path.removeprefix("qdrant-data/") for path in files if path.startswith("qdrant-data/")}:
        raise ValueError("Qdrant data inventory is inconsistent.")


def _copy_tree(source: Path, target: Path, records: dict[str, dict[str, Any]], prefix: str) -> list[str]:
    before = _inventory(source)
    copied: list[str] = []
    for relative in before:
        src = source.joinpath(*PurePosixPath(relative).parts)
        dst = target.joinpath(*PurePosixPath(relative).parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(src, dst, records, f"{prefix}/{relative}", allowed_root=source)
        copied.append(relative)
    after = _inventory(source)
    if before != after:
        raise RuntimeError("Source directory changed while snapshot was being copied; preserve the partial snapshot.")
    return copied


def _inventory(root: Path) -> dict[str, tuple[int, int, int, int, str | None]]:
    result: dict[str, tuple[int, int, int, int, str | None]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                raise ValueError("Source contains a directory symbolic link; only internal file links can be snapshotted.")
        for name in files:
            path = current_path / name
            _resolved, state = _source_file_state(path, root)
            result[path.relative_to(root).as_posix()] = state
    return dict(sorted(result.items()))


def _source_file_state(
    path: Path, allowed_root: Path | None
) -> tuple[Path, tuple[int, int, int, int, str | None]]:
    """Resolve a regular file or a safe internal file symlink without following directory links."""
    link_text = None
    if path.is_symlink():
        if allowed_root is None:
            raise ValueError("Source contains a symbolic link outside an application-data or Qdrant root.")
        link_text = os.readlink(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("Source contains a broken or looping symbolic link.") from None
    if allowed_root is not None:
        try:
            resolved.relative_to(allowed_root)
        except ValueError:
            raise ValueError("Source contains a symbolic link that resolves outside its data root.") from None
        # Directory links are rejected during inventory. Recheck parents to cover a link swap during copying.
        relative = path.relative_to(allowed_root)
        parent = allowed_root
        for part in relative.parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError("Source contains a directory symbolic link; only internal file links can be snapshotted.")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Source contains a non-regular file.")
    return resolved, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, link_text)


def _copy_file(
    source: Path,
    target: Path,
    records: dict[str, dict[str, Any]],
    key: str,
    *,
    allowed_root: Path | None = None,
) -> None:
    digest = hashlib.sha256()
    size = 0
    resolved_source, before_state = _source_file_state(source, allowed_root)
    with resolved_source.open("rb") as src, target.open("xb") as dst:
        while block := src.read(8 * 1024 * 1024):
            dst.write(block)
            digest.update(block)
            size += len(block)
        dst.flush()
        os.fsync(dst.fileno())
    _resolved_after, after_state = _source_file_state(source, allowed_root)
    if size != before_state[2] or before_state != after_state:
        raise RuntimeError("Source file changed while snapshot was being copied; preserve the partial snapshot.")
    records[key] = {"size": size, "sha256": digest.hexdigest()}


def _measure(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _verify_restored_roots(manifest: dict[str, Any], staged: list[tuple[str, Path, Path]]) -> None:
    for name, stage, _target in staged:
        expected = {path: record for path, record in manifest["files"].items() if path.startswith(name + "/")}
        for relative, record in expected.items():
            source_path = stage.joinpath(*PurePosixPath(relative).parts[1:])
            size, digest = _measure(source_path)
            if size != record["size"] or digest != record["sha256"]:
                raise ValueError("Restored file failed integrity verification.")

