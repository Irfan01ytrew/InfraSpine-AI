"""Admin-only parser information and isolated synthetic health tests."""

import asyncio

from fastapi import APIRouter, Depends, Request

from app.api.deps import AppSettings, DbSession, PlatformAdminUser, audit_context, require_platform_admin
from app.rag.parsers.health import parser_catalog, run_parser_health_check
from app.rag.parsers.registry import docling_runtime_enabled
from app.schemas.parsers import DocumentParserInfoResponse, DocumentParserTestResponse, ParserFormat
from app.services import audit

router = APIRouter(
    prefix="/api/admin/document-parsers",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


@router.get("", response_model=DocumentParserInfoResponse)
async def document_parser_info(settings: AppSettings) -> DocumentParserInfoResponse:
    return DocumentParserInfoResponse(
        docling_enabled=docling_runtime_enabled(settings),
        formats=parser_catalog(settings),
    )


@router.post("/{file_format}/test", response_model=DocumentParserTestResponse)
async def test_document_parser(
    file_format: ParserFormat, settings: AppSettings, request: Request,
    db: DbSession, admin: PlatformAdminUser
) -> DocumentParserTestResponse:
    result = await asyncio.to_thread(run_parser_health_check, file_format, settings)
    response = DocumentParserTestResponse(**result)
    audit.record(
        db,
        audit_context(request, admin),
        "admin.document_parser_tested",
        target=file_format,
        details={"ok": response.ok, "parser_used": response.parser_used,
                 "latency_ms": response.latency_ms},
        outcome="success" if response.ok else "failure",
    )
    await db.commit()
    return response
