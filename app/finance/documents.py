"""Invoice intake from documents already indexed in Knowledge.

The person picks documents (or a synced folder) they can *currently* read: the selection is filtered by the live
`readable_by` rule (platform `documents.read` + document ACL), so a document they cannot open is simply "not found".
The indexed text is untrusted data: it is parsed with the same deterministic "Label: value" extractor as uploads and
can only fill invoice fields. It never selects a payee, a permission or an approval target.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import DOC_READY, Document, DocumentChunk
from app.finance import extraction
from app.rag.access import readable_by

MAX_DOCUMENTS = 200
MAX_CHUNKS = 500


def folder_of(document: Document) -> str | None:
    """The folder a synced document came from (`folder-import://<source>/a/b/file.pdf` -> `.../a/b/`)."""
    uri = document.source_uri or ''
    if '://' not in uri or '/' not in uri.split('://', 1)[1]:
        return None
    return uri.rsplit('/', 1)[0] + '/'


def _like_prefix(prefix: str) -> str:
    return prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


async def readable_documents(db, user, *, document_ids=None, folder=None, query=None, limit=MAX_DOCUMENTS):
    """Indexed documents this person can read now, by id and/or folder prefix (or a filename search)."""
    stmt = select(Document).where(readable_by(user), Document.status == DOC_READY)
    if document_ids:
        stmt = stmt.where(Document.id.in_(list(document_ids)))
    if folder:
        if not isinstance(folder, str) or '://' not in folder or len(folder) > 1024:
            raise InvalidInputError('Choose a synced folder.')
        stmt = stmt.where(Document.source_uri.like(_like_prefix(folder), escape='\\'))
    if query:
        stmt = stmt.where(Document.filename.ilike('%' + _like_prefix(query.strip()[:120])[:-1] + '%', escape='\\'))
    return list((await db.scalars(stmt.order_by(Document.filename, Document.id).limit(min(limit, MAX_DOCUMENTS)))).all())


async def indexed_text(db, document: Document) -> str:
    if document.indexed_version != document.version:
        raise ConflictError('Wait until the current document version is indexed.')
    chunks = (await db.scalars(select(DocumentChunk.content).where(DocumentChunk.document_id == document.id)
                               .order_by(DocumentChunk.chunk_index).limit(MAX_CHUNKS))).all()
    return '\n'.join(chunks)[:extraction.MAX_TEXT]


async def facts_from_documents(db, user, *, document_ids=None, folder=None, date_order='dmy'):
    """[(kind, facts, source_ref, digest)] for FinanceService.add_invoices, plus skipped documents with reasons."""
    ids = []
    for value in document_ids or []:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            raise InvalidInputError('Document ids are invalid.') from None
    if not ids and not folder:
        raise InvalidInputError('Choose documents or a folder.')
    if len(ids) > MAX_DOCUMENTS:
        raise InvalidInputError(f'Choose at most {MAX_DOCUMENTS} documents at a time.')
    documents = await readable_documents(db, user, document_ids=ids or None, folder=folder)
    if ids and len({d.id for d in documents}) != len(set(ids)):
        raise NotFoundError('One or more documents were not found.')  # unreadable == not found, no hint which
    items, skipped = [], []
    for document in documents:
        try:
            text = await indexed_text(db, document)
            facts = extraction.facts_from_text(text, date_order=date_order, filename=document.filename)
        except (ConflictError, InvalidInputError) as exc:
            skipped.append({'document_id': str(document.id), 'filename': document.filename, 'reason': exc.message})
            continue
        facts['extraction'] = {**(facts.get('extraction') or {}), 'document_id': str(document.id),
                               'document_version': document.indexed_version}
        digest = extraction.digest_of({'document': str(document.id), 'version': document.indexed_version,
                                       'sha256': document.sha256})
        items.append(('document', facts, f'document:{document.id}', digest))
    return items, skipped
