"""Seed a disposable F02 review database with one synthetic transcript source."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import Document, DocumentChunk, ROLE_ADMIN, User


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    url = make_url(database_url)
    database = url.database or ""
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55928
        or url.username != "postgres"
        or url.password is not None
        or database != "infraspine_f02meetai_20261028_test"
    ):
        raise RuntimeError("F02 seeding is pinned to its new disposable loopback PostgreSQL database.")

    fixture_path = Path(os.environ["INFRASPINE_F02_CREDENTIAL_FIXTURE"]).resolve(strict=True)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    admin = fixture.get("admin")
    if not isinstance(admin, dict) or not admin.get("email") or len(str(admin.get("password", ""))) < 24:
        raise RuntimeError("The protected synthetic Admin fixture is incomplete.")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            existing = await db.scalar(select(func.count()).select_from(User))
            if existing:
                raise RuntimeError("F02 seed database is not empty; preserving it without overwrite.")

            user = User(
                email=str(admin["email"]).casefold(),
                password_hash=hash_password(str(admin["password"])),
                role=ROLE_ADMIN,
                is_active=True,
            )
            db.add(user)
            await db.flush()

            transcript = (
                "The team will review vendor access on November 6. "
                "Jordan Lee will own the review and document unresolved findings in the audit register."
            )
            filename = "f02-synthetic-quarterly-review.mp3"
            source_bytes = b"synthetic transcript fixture; no audio recording is represented"
            document = Document(
                owner_id=user.id,
                visibility="shared",
                source_system="upload",
                filename=filename,
                file_format="audio",
                storage_key=uuid.uuid4().hex,
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                size_bytes=len(source_bytes),
                version=1,
                status="ready",
                chunk_count=1,
                indexed_version=1,
                indexed_filename=filename,
                embedding_model="synthetic-f02-transcript-fixture",
                indexed_at=datetime.now(UTC),
            )
            db.add(document)
            await db.flush()
            db.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=0,
                    page_number=1,
                    locator={
                        "kind": "audio",
                        "label": "00:00:15–00:00:24",
                        "attrs": {"start_seconds": 15.0, "end_seconds": 24.0},
                    },
                    content=transcript,
                    embedding=bytes(384 * 4),
                )
            )
            await db.commit()
    finally:
        await engine.dispose()

    print("F02_SYNTHETIC_TRANSCRIPT_SEEDED=1")


if __name__ == "__main__":
    asyncio.run(main())
