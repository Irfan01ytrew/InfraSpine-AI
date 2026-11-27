"""Add one durable, pre-indexed synthetic PDF source to the isolated F02 review DB."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.models import Document, DocumentChunk, User

DATABASE = "infraspine_f02meetai_20261028_test"
FILENAME = "c09-conflict-review-source-20261028.pdf"
SOURCE_TEXT = (
    "Synthetic customer device handling policy: Staff may take corporate computers home. "
    "Department personnel must record asset movement in the source register."
)


def _make_pdf(text: str) -> bytes:
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
    )
    page = writer.add_blank_page(595, 842)
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 50 790 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


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
        raise RuntimeError("C09 browser source seeding is pinned to the isolated F02 review database.")
    data_root = Path(os.environ["INFRASPINE_C09_REVIEW_ROOT"]).resolve(strict=True)
    expected_root = (ROOT / ".verification-local-review-20261028-f02-meetings").resolve(strict=True)
    if data_root != expected_root:
        raise RuntimeError("C09 browser source seeding is pinned to the retained synthetic F02 data root.")
    fixture_path = Path(os.environ["INFRASPINE_F03_CREDENTIAL_FIXTURE"]).resolve(strict=True)
    if fixture_path != expected_root / "f03-credentials.json":
        raise RuntimeError("C09 browser source seeding requires the protected synthetic F03 role fixture.")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    admin_email = str(fixture.get("admin", {}).get("email", "")).casefold()
    if not admin_email:
        raise RuntimeError("The protected synthetic Admin fixture is incomplete.")

    uploads = data_root / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    content = _make_pdf(SOURCE_TEXT)
    digest = hashlib.sha256(content).hexdigest()
    storage_key = f"{hashlib.sha256(FILENAME.encode()).hexdigest()[:32]}.pdf"
    stored = uploads / storage_key
    if stored.exists():
        if hashlib.sha256(stored.read_bytes()).hexdigest() != digest:
            raise RuntimeError("A C09 synthetic source file already exists with different bytes; preserving it.")
    else:
        with stored.open("xb") as handle:
            handle.write(content)

    engine = create_async_engine(str(url), pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            admin = await db.scalar(select(User).where(User.email == admin_email))
            if admin is None or admin.role != "admin" or not admin.is_active:
                raise RuntimeError("The isolated synthetic Admin account is unavailable.")
            document = await db.scalar(select(Document).where(Document.filename == FILENAME))
            if document is None:
                document = Document(
                    owner_id=admin.id,
                    visibility="shared",
                    filename=FILENAME,
                    file_format="pdf",
                    storage_key=storage_key,
                    sha256=digest,
                    size_bytes=len(content),
                    status="ready",
                    page_count=1,
                    chunk_count=1,
                    indexed_version=1,
                    indexed_filename=FILENAME,
                    embedding_model="sentence-transformers/all-MiniLM-L6-v2",
                    indexed_at=datetime.now(UTC),
                )
                db.add(document)
                await db.flush()
            elif (
                document.owner_id != admin.id
                or document.visibility != "shared"
                or document.sha256 != digest
                or document.storage_key != storage_key
                or document.status != "ready"
                or document.indexed_version != document.version
            ):
                raise RuntimeError("The retained C09 synthetic source metadata differs; preserving it.")

            chunk = await db.scalar(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id, DocumentChunk.chunk_index == 0)
            )
            if chunk is None:
                db.add(
                    DocumentChunk(
                        document_id=document.id,
                        chunk_index=0,
                        page_number=1,
                        locator={"kind": "page", "page": 1},
                        content=SOURCE_TEXT,
                        embedding=np.zeros((384,), dtype=np.float32).tobytes(),
                    )
                )
            elif chunk.content != SOURCE_TEXT:
                raise RuntimeError("The retained C09 synthetic source passage differs; preserving it.")
            await db.commit()
    finally:
        await engine.dispose()

    print("C09_PREINDEXED_SYNTHETIC_PDF_SOURCE=READY; bytes and SHA-256 match retained file")


if __name__ == "__main__":
    asyncio.run(main())
