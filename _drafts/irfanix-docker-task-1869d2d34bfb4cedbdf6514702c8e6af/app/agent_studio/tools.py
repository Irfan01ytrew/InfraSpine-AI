"""Bounded, explicit tools exposed to Agent Studio graphs."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher, EncryptedValue
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioMemory
from app.db.models import ROLE_ADMIN, AutomationTask, PersonalTask, User
from app.services.chat_exports import create_chat_export
from app.services.chat_images import create_chat_image
from app.services.llm import LLMClient
from app.services.public_web import public_web_research
from app.services import automation as automation_service
from app.services import personal_tasks as personal_tasks_service


TOOL_DEFINITIONS = (
    ("project_file_run_python", "Execute a standalone Python check offline in an isolated WSL namespace. Only the selected source and Linux standard library are available.", False),
    ("web_search", "Search the public web and return current source excerpts with URLs.", False),
    ("browse_url", "Safely fetch and extract text from a public HTTPS URL.", False),
    ("browser_capture", "Open an allowed public site in an isolated, no-cookie, GET-only browser and extract visible text.", False),
    ("file_list", "List files in this agent's private workspace.", False),
    ("file_read", "Read a bounded UTF-8 text file from this agent's private workspace.", False),
    ("project_file_list", "List source files in the owner's allow-listed InfraSpine code folders; runtime data and credentials are excluded.", False),
    ("project_file_check_python", "Check the syntax of one project Python file and return its SHA-256 digest. Does not run the file or its tests.", False),
    ("project_file_read", "Read a bounded UTF-8 InfraSpine source file from an allow-listed project folder.", False),
    ("project_file_replace", "Apply one exact unique source-snippet replacement in an allow-listed InfraSpine file and save a recoverable backup.", False),
    ("project_file_restore", "Restore one source file from a backup created by project_file_replace; saves the current version first.", False),
    ("project_docker_run_pytest", "Run approved pytest checks or up to eight exact pytest targets in a writable, disposable Docker source snapshot with an isolated temporary PostgreSQL database and no public network.", False),
    ("create_file", "Create a downloadable PPTX, DOCX, XLSX, or PDF from prepared content.", True),
    ("generate_image", "Generate an image through the configured image-capable AI provider.", True),
    ("rag_search", "Optionally search the user's authorized company knowledge library.", False),
    ("my_work", "List the authenticated user's own open assigned work tasks and due dates. Never show another user's tasks.", False),
    ("remember", "Save a private encrypted preference or project fact for this agent.", False),
    ("recall", "Retrieve this agent's private saved memories.", False),
    ("delegate_agent", "Delegate a bounded subtask to one of this agent's explicitly allowed subagents.", False),
)

TOOL_NAMES = frozenset(row[0] for row in TOOL_DEFINITIONS)
TOOL_DESCRIPTIONS = {name: description for name, description, _approval in TOOL_DEFINITIONS}
TOOL_ARGUMENT_SCHEMAS = {
    "project_file_run_python": {"path": "allow-listed standalone Python check"},
    "web_search": {"query": "string: the public web question to research"},
    "browse_url": {"url": "string: one public HTTPS page URL"},
    "browser_capture": {"url": "string: one public URL on an administrator-allowed origin"},
    "file_list": {},
    "file_read": {"name": "string: relative UTF-8 text file path inside this agent's workspace"},
    "project_file_list": {"prefix": "optional string: source folder prefix such as app/rag or frontend/src"},
    "project_file_check_python": {"path": "string: allow-listed project-relative Python path"},
    "project_file_read": {"path": "string: allow-listed project-relative source path"},
    "project_file_replace": {"path": "string: allow-listed project-relative source path", "old_text": "string: exact unique snippet, maximum 6,000 chars", "new_text": "string: replacement snippet, maximum 6,000 chars"},
    "project_file_restore": {"path": "string: allow-listed project-relative source path", "backup_id": "string: 32-character backup ID returned by project_file_replace"},
    "project_docker_run_pytest": {"target": "string: tests, an allow-listed tests/test_*.py file, or up to eight comma-separated exact ::test_name selectors; arbitrary shell commands are not accepted"},
    "create_file": {
        "format": "string enum: pptx, docx, xlsx, pdf",
        "content": "string: complete content to place in the downloadable file",
    },
    "generate_image": {"prompt": "string: a detailed image generation prompt"},
    "rag_search": {"query": "string: question for the user's ACL-filtered company library"},
    "my_work": {},
    "remember": {"memory_key": "string: short stable preference or project fact key", "value": "string: private memory value"},
    "recall": {},
    "delegate_agent": {"agent_id": "string: UUID of an explicitly allowed subagent", "task": "string: bounded delegated subtask"},
}
APPROVAL_TOOLS = frozenset({"create_file", "generate_image"})
PROJECT_WRITE_TOOLS = frozenset({"project_file_replace", "project_file_restore", "project_file_run_python", "project_docker_run_pytest"})


def _string_arg(arguments: dict[str, Any], name: str, *, max_length: int = 6000) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"Provide a non-empty {name} (maximum {max_length} characters).")
    return value.strip()


def _workspace(settings: Settings, user_id: uuid.UUID, agent_id: uuid.UUID) -> Path:
    base = (settings.rag_data_dir / "agent_studio").resolve()
    root = (base / str(user_id) / str(agent_id)).resolve()
    if root.parent.parent != base:
        raise ValueError("Agent workspace path is outside the configured storage root.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_file(root: Path, name: str) -> Path:
    if len(name) > 240 or not name.strip() or "\x00" in name:
        raise ValueError("The file name is invalid.")
    candidate = (root / name).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("The requested path is outside this agent's workspace.")
    return candidate


def _memory_aad(memory_id: uuid.UUID) -> bytes:
    return f"infraspine:agent-studio-memory:v1:{memory_id}".encode("ascii")


def _encrypt(cipher: CredentialCipher, memory_id: uuid.UUID, value: str) -> EncryptedValue:
    return cipher.encrypt(value, associated_data=_memory_aad(memory_id))


def _decrypt(cipher: CredentialCipher, row: AgentStudioMemory) -> str:
    return cipher.decrypt(
        EncryptedValue(row.value_ciphertext, row.value_nonce, row.value_key_id),
        associated_data=_memory_aad(row.id),
    )


async def run_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: AsyncSession,
    user: User,
    agent: AgentStudioAgent,
    settings: Settings,
    cipher: CredentialCipher,
    llm: LLMClient,
    rag: Any | None,
) -> dict[str, Any]:
    """Run only a registered tool. Every filesystem path, URL and result has a bounded scope."""
    if name not in TOOL_NAMES:
        raise ValueError("This tool is not registered.")

    if name == "web_search":
        query = _string_arg(arguments, "query", max_length=1000)
        result = await public_web_research(llm, settings, query)
        sources = list(result.get("sources") or [])[:8]
        return {
            "answer": str(result.get("answer") or "")[:12000],
            "sources": [{**source, "citation": f"[S{index}]"} for index, source in enumerate(sources, 1)],
        }

    if name == "browse_url":
        url = _string_arg(arguments, "url", max_length=2048)
        result = await public_web_research(llm, settings, f"Extract and summarize the relevant page content at {url}", urls=[url])
        sources = list(result.get("sources") or [])[:5]
        return {
            "answer": str(result.get("answer") or "")[:10000],
            "sources": [{**source, "citation": f"[S{index}]"} for index, source in enumerate(sources, 1)],
        }

    if name == "browser_capture":
        url = _string_arg(arguments, "url", max_length=2048)
        from app.services.legacy_browser import capture_readonly

        captured = await capture_readonly(settings, url, [])
        return {
            "pages": [
                {"title": page.title[:300], "url": page.url, "text": page.text[:6000]}
                for page in captured.pages[:4]
            ],
            "requests": captured.request_count,
            "bytes": captured.response_bytes,
        }

    if name.startswith(("project_file_", "project_docker_")):
        if user.role != ROLE_ADMIN:
            raise ValueError("Project source access requires an administrator account.")
        from app.agent_studio.project_tools import run_project_tool

        return await run_project_tool(name, arguments, settings=settings, user_id=user.id, agent_id=agent.id)

    if name in {"file_list", "file_read"}:
        root = _workspace(settings, user.id, agent.id)
        if name == "file_list":
            def list_files():
                results = []
                for path in sorted(root.rglob("*")):
                    resolved = path.resolve()
                    if path.is_file() and root in resolved.parents:
                        size = path.stat().st_size
                        results.append({"name": str(path.relative_to(root)), "bytes": size})
                    if len(results) >= 100:
                        break
                return results

            return {"files": await asyncio.to_thread(list_files)}

        requested = _string_arg(arguments, "name", max_length=240)
        path = _workspace_file(root, requested)
        if not path.is_file():
            raise ValueError("That file does not exist in this agent's workspace.")
        if path.stat().st_size > 1_000_000:
            raise ValueError("Files larger than 1 MB cannot be read as text.")
        content = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="strict")
        return {"name": str(path.relative_to(root)), "content": content[:12000]}

    if name == "create_file":
        content = _string_arg(arguments, "content", max_length=40000)
        file_format = _string_arg(arguments, "format", max_length=8).lower().lstrip(".")
        if file_format not in {"pptx", "docx", "xlsx", "pdf"}:
            raise ValueError("Choose pptx, docx, xlsx or pdf.")
        token, file_name = await asyncio.to_thread(
            create_chat_export,
            content,
            file_format,
            settings.rag_data_dir / "exports",
            user.id,
            settings.master_key_bytes,
        )
        return {
            "file_name": file_name,
            "format": file_format,
            "download_url": f"/api/exports/download/{token}?format={file_format}",
        }

    if name == "generate_image":
        prompt = _string_arg(arguments, "prompt", max_length=2000)
        from app.services.local_image_generation import generate_image_with_local_fallback, normalize_image_prompt

        async def current_client():
            return llm

        image_bytes, _mime, _model, _provider = await generate_image_with_local_fallback(
            settings, normalize_image_prompt(prompt), current_client,
        )
        token = await asyncio.to_thread(
            create_chat_image,
            image_bytes,
            settings.rag_data_dir / "generated_images",
            user.id,
            settings.master_key_bytes,
        )
        return {
            "image_url": f"/api/chat-images/{token}",
            "download_url": f"/api/chat-images/{token}?download=true",
        }

    if name == "rag_search":
        if not agent.rag_enabled or rag is None:
            raise ValueError("RAG access is not enabled for this agent.")
        query = _string_arg(arguments, "query", max_length=1000)
        from app.rag.retrieval import retrieve

        chunks = await retrieve(
            db,
            user,
            rag.embedder,
            rag.vector_store,
            query,
            top_k=6,
            min_similarity=0.15,
            max_context_chars=10000,
            hybrid=True,
            max_per_document=3,
            max_documents=5,
        )
        return {
            "sources": [
                {
                    "citation": f"[S{index}]",
                    "filename": chunk.filename,
                    "location": chunk.location_label,
                    "text": chunk.content[:4000],
                }
                for index, chunk in enumerate(chunks, 1)
            ]
        }

    if name == "my_work":
        # Reuse the platform task services so department scope and ownership rules
        # remain identical to the My Work UI. Only tasks assigned to this caller are
        # returned, even for Admins and department managers.
        personal = await personal_tasks_service.list_for_user(db, user)
        departmental = await automation_service.list_tasks(db, user)
        now = datetime.now(UTC)
        tasks: list[tuple[datetime | None, dict[str, Any]]] = []
        for task in personal:
            if task.assignee_user_id != user.id or task.status != "open":
                continue
            tasks.append((task.due_at, {
                "title": task.title,
                "details": task.body[:1200],
                "source": "My Work",
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "overdue": bool(task.due_at and task.due_at < now),
            }))
        for task in departmental:
            if task.assignee_user_id != user.id or task.status != "open":
                continue
            tasks.append((task.due_at, {
                "title": task.title,
                "details": task.body[:1200],
                "source": "Department work",
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "overdue": bool(task.due_at and task.due_at < now),
            }))
        tasks.sort(key=lambda row: row[0] or datetime.max.replace(tzinfo=UTC))
        return {"count": len(tasks), "tasks": [item for _, item in tasks[:60]], "checked_at": now.isoformat()}

    if name == "remember":
        memory_key = _string_arg(arguments, "memory_key", max_length=100).casefold()
        if not re.fullmatch(r"[\w .:/-]{1,100}", memory_key, flags=re.UNICODE):
            raise ValueError("The memory key contains unsupported characters.")
        value = _string_arg(arguments, "value", max_length=1200)
        memory_id = uuid.uuid5(uuid.NAMESPACE_URL, f"infraspine-agent-memory:{user.id}:{agent.id}:{memory_key}")
        encrypted = _encrypt(cipher, memory_id, value)
        await db.execute(
            pg_insert(AgentStudioMemory)
            .values(
                id=memory_id,
                agent_id=agent.id,
                owner_id=user.id,
                memory_key=memory_key,
                value_ciphertext=encrypted.ciphertext,
                value_nonce=encrypted.nonce,
                value_key_id=encrypted.key_id,
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                constraint="uq_agent_studio_memory_key",
                set_={
                    "value_ciphertext": encrypted.ciphertext,
                    "value_nonce": encrypted.nonce,
                    "value_key_id": encrypted.key_id,
                    "updated_at": datetime.now(UTC),
                },
            )
        )
        return {"saved": True, "memory_key": memory_key}

    if name == "recall":
        rows = (
            await db.execute(
                select(AgentStudioMemory)
                .where(AgentStudioMemory.agent_id == agent.id, AgentStudioMemory.owner_id == user.id)
                .order_by(AgentStudioMemory.updated_at.desc())
                .limit(20)
            )
        ).scalars()
        return {"memories": [{"memory_key": row.memory_key, "value": _decrypt(cipher, row)} for row in rows]}

    raise ValueError("This tool must be executed by the agent orchestration service.")
