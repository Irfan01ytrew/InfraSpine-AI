"""Seed one synthetic, department-restricted processed transcript for F02 role-flow QA."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.models import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    DocumentChunk,
    ResourceAcl,
    User,
)


DATABASE = "infraspine_f02meetai_20261028_test"
GROUP_NAME = "F03 Knowledge Interview Operations"
FILENAME = "f02-synthetic-manager-review-transcript.wav"
TRANSCRIPT = (
    "The team approved a review of vendor access by November 6. "
    "The meeting owner will update the audit register and report unresolved findings."
)


def _read_fixture(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    manager = value.get("managers")
    employees = value.get("employees")
    if not isinstance(manager, list) or len(manager) != 1 or not isinstance(employees, list) or len(employees) != 2:
        raise RuntimeError("The protected F03 synthetic role fixture has an unexpected shape.")
    if any(not row.get("email") or len(str(row.get("password", ""))) < 24 for row in [manager[0], *employees]):
        raise RuntimeError("The protected F03 synthetic role fixture is incomplete.")
    return value


async def main() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55928
        or url.username != "postgres"
        or url.password is not None
        or url.database != DATABASE
    ):
        raise RuntimeError("F02 review-workflow fixtures are pinned to the isolated loopback QA database.")

    fixture_path = Path(os.environ["INFRASPINE_F03_CREDENTIAL_FIXTURE"]).resolve(strict=True)
    manifest_path = Path(os.environ["INFRASPINE_F02_WORKFLOW_MANIFEST"]).resolve()
    fixture = _read_fixture(fixture_path)
    engine = create_async_engine(str(url), pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            manager = await db.scalar(select(User).where(User.email == fixture["managers"][0]["email"].casefold()))
            author = await db.scalar(select(User).where(User.email == fixture["employees"][0]["email"].casefold()))
            outsider = await db.scalar(select(User).where(User.email == fixture["employees"][1]["email"].casefold()))
            admin = await db.scalar(select(User).where(User.email == fixture["admin"]["email"].casefold()))
            group = await db.scalar(select(AccessGroup).where(AccessGroup.name == GROUP_NAME))
            if (
                manager is None or manager.role != ROLE_MANAGER or not manager.is_active
                or author is None or author.role != ROLE_EMPLOYEE or not author.is_active
                or outsider is None or outsider.role != ROLE_EMPLOYEE or not outsider.is_active
                or admin is None or admin.role != ROLE_ADMIN or group is None
            ):
                raise RuntimeError("The isolated F02/F03 reviewer fixture is unavailable; nothing was changed.")
            if await db.get(AccessGroupMember, (group.id, author.id)) is None:
                raise RuntimeError("The interview author is outside the required synthetic department.")
            if await db.get(DepartmentManager, (group.id, manager.id)) is None:
                raise RuntimeError("The reviewer does not manage the required synthetic department.")

            existing = await db.scalar(select(Document).where(Document.filename == FILENAME))
            if existing is None:
                source_bytes = b"synthetic preprocessed transcript metadata; no audio upload is represented"
                source_hash = hashlib.sha256(source_bytes).hexdigest()
                document = Document(
                    owner_id=author.id,
                    visibility="restricted",
                    source_system="upload",
                    filename=FILENAME,
                    file_format="audio",
                    storage_key=uuid.uuid4().hex,
                    sha256=source_hash,
                    size_bytes=len(source_bytes),
                    version=1,
                    status="ready",
                    chunk_count=1,
                    indexed_version=1,
                    indexed_filename=FILENAME,
                    embedding_model="synthetic-f02-review-transcript",
                    indexed_at=datetime.now(UTC),
                )
                db.add(document)
                await db.flush()
                chunk = DocumentChunk(
                    document_id=document.id,
                    chunk_index=0,
                    page_number=1,
                    locator={
                        "kind": "audio",
                        "label": "00:00:12–00:00:22",
                        "attrs": {"start_seconds": 12.0, "end_seconds": 22.0},
                    },
                    content=TRANSCRIPT,
                    embedding=bytes(384 * 4),
                )
                db.add(chunk)
                db.add(ResourceAcl(
                    resource_type="document",
                    document_id=document.id,
                    principal_group_id=group.id,
                    origin="manual",
                    created_by=admin.id,
                ))
                await db.flush()
            else:
                document = existing
                chunk = await db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == document.id))
                acl = await db.scalar(select(ResourceAcl).where(
                    ResourceAcl.document_id == document.id,
                    ResourceAcl.principal_group_id == group.id,
                ))
                if (
                    document.owner_id != author.id or document.visibility != "restricted"
                    or document.status != "ready" or document.file_format != "audio"
                    or chunk is None or chunk.content != TRANSCRIPT or acl is None
                ):
                    raise RuntimeError("The retained synthetic F02 transcript differs; preserving it without overwrite.")

            await db.commit()
            manifest = {
                "database": DATABASE,
                "migration": "0059_workspace_hierarchy",
                "source_document_id": str(document.id),
                "source_filename": document.filename,
                "transcript_chunk_id": chunk.id,
                "department_scope": GROUP_NAME,
                "source_note": "synthetic processed-transcript database fixture; no media upload or worker run",
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    finally:
        await engine.dispose()

    print("F02_SYNTHETIC_RESTRICTED_REVIEW_SOURCE=READY")


if __name__ == "__main__":
    asyncio.run(main())
