"""Build bounded deterministic finance extraction payloads from current indexed source data."""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError
from app.db.models import DOC_READY, Document, DocumentChunk, DocumentTable
from app.rag.financial_forms import (
    MAX_LINE_ITEMS,
    MAX_ROWS_PER_TABLE,
    MAX_TABLES,
    MAX_TEXT_CHARS,
    detect_form_profile,
    extract_financial_form,
)
from app.rag.parsers.base import ExtractedTable


async def extract_financial_payload(
    document: Document,
    db: AsyncSession,
    profile: Literal["auto", "invoice", "purchase_order", "form"],
    date_order: Literal["dmy", "mdy", "ymd"],
) -> dict:
    """Compute source-linked values without a model and reject stale or unindexed documents."""
    if document.status != DOC_READY or document.indexed_version != document.version:
        raise ConflictError("Wait until the current document version is indexed before extracting fields.")

    chunks = list((await db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index)
        .limit(501)
    )).all())
    text_sections: list[tuple[str, dict | None]] = []
    used_chars = 0
    for chunk in chunks[:500]:
        remaining = MAX_TEXT_CHARS + 1 - used_chars
        if remaining <= 0:
            break
        content = chunk.content[:remaining]
        text_sections.append((content, chunk.locator or {"kind": "page", "position": chunk.page_number}))
        used_chars += len(content)

    table_rows = list((await db.scalars(
        select(DocumentTable)
        .where(DocumentTable.document_id == document.id)
        .order_by(DocumentTable.table_index)
        .limit(MAX_TABLES + 1)
    )).all())
    tables: list[ExtractedTable] = []
    for row in table_rows:
        rows = row.rows if isinstance(row.rows, list) else []
        positions = row.row_positions if isinstance(row.row_positions, list) else []
        summaries = row.summary_rows if isinstance(row.summary_rows, list) else []
        tables.append(ExtractedTable(
            index=row.table_index,
            title=row.title,
            kind=row.locator_kind,
            columns=tuple(str(value) for value in (row.columns or [])),
            rows=tuple(tuple(str(value) for value in values) for values in rows[:MAX_ROWS_PER_TABLE + 1]),
            row_positions=tuple(int(value) for value in positions[:MAX_ROWS_PER_TABLE + 1]),
            summary_rows=tuple(tuple(str(value) for value in values) for values in summaries[:MAX_ROWS_PER_TABLE + 1]),
            structured=row.structured,
            note=row.note,
        ))

    all_text = "\n".join(section for section, _ in text_sections)
    selected_profile = detect_form_profile(document.filename, all_text) if profile == "auto" else profile
    if selected_profile is None:
        raise InvalidInputError("No invoice or purchase-order profile was detected. Choose a profile or select generic form.")
    extraction = extract_financial_form(selected_profile, text_sections, tables, date_order=date_order)
    return {
        "document_id": str(document.id),
        "filename": document.filename,
        "document_version": document.indexed_version,
        "profile": extraction.profile,
        "date_order": date_order,
        "status": extraction.status,
        "fields": {name: {
            "value": field.value,
            "raw": field.raw,
            "valid": field.valid,
            "source": field.source,
        } for name, field in extraction.fields.items()},
        "line_items": list(extraction.line_items[:MAX_LINE_ITEMS]),
        "exceptions": [{
            "code": issue.code,
            "field": issue.field,
            "message": issue.message,
            "severity": issue.severity,
            "source": issue.source,
        } for issue in extraction.exceptions],
        "note": "Review extracted values against the original document before using them for payment or posting.",
    }


def overlay_reviewed_financial_values(extracted: dict, review) -> dict:
    """Return the latest human corrections while retaining immutable extraction source locations."""
    visible = dict(extracted)
    if review is None:
        return visible
    visible["fields"] = {
        name: {**field, "value": review.fields.get(name)}
        for name, field in extracted.get("fields", {}).items()
    }
    visible["line_items"] = [
        {**source_item, **{name: reviewed_item.get(name) for name in ("description", "quantity", "unit_price", "amount", "currency")}}
        for source_item, reviewed_item in zip(
            extracted.get("line_items", []), review.line_items, strict=True
        )
    ]
    return visible
