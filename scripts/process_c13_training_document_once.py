"""Process exactly one protected synthetic C13 source without running unrelated worker schedules."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, DocumentChunk
from app.db.session import create_engine, create_sessionmaker
from app.rag.components import build_worker


async def main() -> None:
    root = Path(os.environ["INFRASPINE_C13_ROOT"]).resolve(strict=True)
    upload_evidence = json.loads((root / "evidence" / "c13-source-upload.json").read_text(encoding="utf-8"))
    database_url = os.environ["DATABASE_URL"]
    expected = "postgresql+asyncpg://postgres@127.0.0.1:55929/infraspine_c11dept_20261029_test"
    if database_url != expected or upload_evidence.get("result") != "PASS":
        raise RuntimeError("Refusing to use anything except the isolated C11/C13 QA database and accepted upload.")
    document_id = UUID(upload_evidence["documentId"])
    settings = get_settings()
    expected_data = (root / "data").resolve(strict=True)
    if settings.rag_data_dir.resolve(strict=True) != expected_data:
        raise RuntimeError("The API and one-shot worker do not share the isolated C13 data directory.")
    engine = create_engine(database_url, pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    worker = build_worker(settings, sessionmaker)
    try:
        async with sessionmaker() as db:
            queued = list((await db.scalars(select(Document).where(Document.status.in_(("pending", "processing"))))).all())
            if len(queued) != 1 or queued[0].id != document_id or queued[0].status != "pending":
                raise RuntimeError("The isolated queue does not contain exactly the one expected pending C13 file.")
            if queued[0].sha256 != upload_evidence["sourceSha256"]:
                raise RuntimeError("The uploaded C13 file hash differs from the protected source fixture.")

        await worker.startup()
        worked = await worker.process_next()
        if not worked:
            raise RuntimeError("The isolated worker did not claim the single C13 source.")
        async with sessionmaker() as db:
            document = await db.get(Document, document_id)
            chunks = list((await db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document_id))).all())
            if document is None or document.status != "ready" or document.indexed_version != document.version or not chunks:
                raise RuntimeError("The one-shot C13 document did not finish as a searchable source.")
            source_text = " ".join(chunk.content for chunk in chunks)
            required = (
                "Recovery tokens must be rotated every 90 days.",
                "Verify the recovery owner before issuing a replacement token.",
                "Record the renewal date after rotation.",
            )
            if any(sentence not in source_text for sentence in required):
                raise RuntimeError("The processed C13 chunks do not match the synthetic source sentences.")
            source_path = settings.rag_data_dir / "uploads" / document.storage_key
            actual_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if actual_sha != document.sha256 or actual_sha != upload_evidence["sourceSha256"]:
                raise RuntimeError("The original C13 source hash does not match API metadata.")
            result = {
                "result": "PASS",
                "documentId": str(document.id),
                "filename": document.filename,
                "status": document.status,
                "version": document.version,
                "indexedVersion": document.indexed_version,
                "chunkCount": len(chunks),
                "sourceBytes": source_path.stat().st_size,
                "sourceSha256": actual_sha,
                "sourceSentencesVerified": len(required),
                "dataDirectory": str(expected_data),
            }
        (root / "evidence" / "c13-source-worker.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(f"C13_SINGLE_DOCUMENT_WORKER=PASS; status=ready; chunks={len(chunks)}; unrelated schedules were not run")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
