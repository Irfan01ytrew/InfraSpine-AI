"""Admin-only parser information and isolated synthetic health tests."""

import asyncio

from fastapi import APIRouter, Depends

from app.api.deps import AppSettings, require_admin
from app.rag.parsers.health import parser_catalog, run_parser_health_check
from app.rag.parsers.registry import docling_runtime_enabled
from app.schemas.parsers import DocumentParserInfoResponse, DocumentParserTestResponse, ParserFormat

router = APIRouter(
    prefix="/api/admin/document-parsers",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=DocumentParserInfoResponse)
async def document_parser_info(settings: AppSettings) -> DocumentParserInfoResponse:
    return DocumentParserInfoResponse(
        docling_enabled=docling_runtime_enabled(settings),
        formats=parser_catalog(settings),
    )


@router.post("/{file_format}/test", response_model=DocumentParserTestResponse)
async def test_document_parser(file_format: ParserFormat, settings: AppSettings) -> DocumentParserTestResponse:
    result = await asyncio.to_thread(run_parser_health_check, file_format, settings)
    return DocumentParserTestResponse(**result)
