"""Narrow, reversible source access for the owner's InfraSpine project agent."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_ROOTS = {"app", "frontend/src", "migrations/versions", "tests", "scripts", "integrations/whatsapp-baileys", "docs"}
_BLOCKED = {".git", ".venv", "node_modules", "data", "logs", ".backups", ".local-db-backups", "artifacts"}
_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".json", ".sql", ".md", ".txt", ".yml", ".yaml", ".toml"}
_MAX_FILE = 1_000_000


def _path(raw: Any) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 400:
        raise ValueError("Provide a project-relative source path.")
    value = raw.strip().replace("\\", "/")
    candidate_text = Path(value)
    parts = value.split("/")
    if candidate_text.is_absolute() or ":" in value or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("The path must stay inside an allowed project source folder.")
    if any(part.casefold() in _BLOCKED or part.casefold().startswith(".env") or "secret" in part.casefold() for part in parts):
        raise ValueError("Credentials, runtime data and dependency folders are outside this agent's source access.")
    if len(parts) < 2 or not any(value == root or value.startswith(root + "/") for root in _ALLOWED_ROOTS):
        raise ValueError("This path is outside the allow-listed project source folders.")
    path = (_ROOT / Path(*parts)).resolve()
    if _ROOT not in path.parents or path.suffix.casefold() not in _EXTENSIONS:
        raise ValueError("This path is outside the allow-listed project source files.")
    return value, path


def _list(prefix: str = "") -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    roots = [(_ROOT / root).resolve() for root in sorted(_ALLOWED_ROOTS)]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            relative_parts = path.relative_to(_ROOT).parts
            if any(part.casefold() in _BLOCKED or part.casefold().startswith(".env") for part in relative_parts):
                continue
            if not path.is_file() or path.suffix.casefold() not in _EXTENSIONS:
                continue
            relative = path.relative_to(_ROOT).as_posix()
            if prefix and not relative.startswith(prefix):
                continue
            try:
                size = path.stat().st_size
                if size <= _MAX_FILE:
                    files.append({"path": relative, "bytes": size})
            except OSError:
                continue
            if len(files) >= 300:
                return {"files": files, "limit_reached": True}
    return {"files": files, "limit_reached": False}


def _read(relative: str) -> dict[str, Any]:
    shown, path = _path(relative)
    if not path.is_file():
        raise ValueError("That source file does not exist.")
    if path.stat().st_size > _MAX_FILE:
        raise ValueError("Source files larger than 1 MB are not available through this tool.")
    text = path.read_text(encoding="utf-8", errors="strict")
    return {"path": shown, "content": text[:12000], "truncated": len(text) > 12000}


def _replace(relative: str, old_text: str, new_text: str, settings, user_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    shown, path = _path(relative)
    if not path.is_file() or path.stat().st_size > _MAX_FILE:
        raise ValueError("The source file must exist and be at most 1 MB.")
    if not isinstance(old_text, str) or not old_text or len(old_text) > 6000:
        raise ValueError("Provide a non-empty, exact source snippet (maximum 6,000 characters).")
    if not isinstance(new_text, str) or len(new_text) > 6000:
        raise ValueError("Replacement text must be at most 6,000 characters.")
    original = path.read_text(encoding="utf-8", errors="strict")
    occurrences = original.count(old_text)
    if occurrences != 1:
        raise ValueError(f"Safe edit stopped: the exact snippet must occur once; found {occurrences}.")
    backup_id = uuid.uuid4().hex
    backup_root = (settings.rag_data_dir / "agent_studio" / "repo_backups" / str(user_id) / str(agent_id)).resolve()
    base = (settings.rag_data_dir / "agent_studio").resolve()
    if base not in backup_root.parents:
        raise ValueError("Could not construct a safe source backup location.")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{backup_id}.bak"
    shutil.copy2(path, backup)
    content = original.replace(old_text, new_text, 1)
    fd, temporary = tempfile.mkstemp(prefix=".codex-edit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {"path": shown, "changed": True, "backup_id": backup_id, "note": "One unique exact replacement was applied. Review and run the relevant checks before deployment."}


def _restore(relative: str, backup_id: str, settings, user_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    shown, path = _path(relative)
    if not isinstance(backup_id, str) or len(backup_id) != 32 or any(ch not in "0123456789abcdef" for ch in backup_id.casefold()):
        raise ValueError("Provide the 32-character backup ID returned by a project edit.")
    base = (settings.rag_data_dir / "agent_studio").resolve()
    backup_root = (base / "repo_backups" / str(user_id) / str(agent_id)).resolve()
    backup = backup_root / f"{backup_id}.bak"
    if base not in backup_root.parents or not backup.is_file() or backup.stat().st_size > _MAX_FILE:
        raise ValueError("That project backup does not exist or is too large.")
    recovery_id = uuid.uuid4().hex
    shutil.copy2(path, backup_root / f"{recovery_id}.bak")
    fd, temporary = tempfile.mkstemp(prefix=".codex-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle, backup.open("rb") as source:
            shutil.copyfileobj(source, handle, _MAX_FILE + 1)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {"path": shown, "restored": True, "recovery_backup_id": recovery_id}


async def run_project_tool(name: str, arguments: dict[str, Any], *, settings, user_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    if name == "project_file_list":
        prefix = arguments.get("prefix", "")
        if not isinstance(prefix, str) or len(prefix) > 200:
            raise ValueError("The optional source prefix is invalid.")
        return await asyncio.to_thread(_list, prefix.replace("\\", "/"))
    if name == "project_file_read":
        return await asyncio.to_thread(_read, arguments.get("path"))
    if name == "project_file_replace":
        return await asyncio.to_thread(_replace, arguments.get("path"), arguments.get("old_text"), arguments.get("new_text"), settings, user_id, agent_id)
    if name == "project_file_restore":
        return await asyncio.to_thread(_restore, arguments.get("path"), arguments.get("backup_id"), settings, user_id, agent_id)
    raise ValueError("Unknown project source tool.")
