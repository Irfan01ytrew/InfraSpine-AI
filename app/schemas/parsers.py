"""Admin-facing status and health-test results for the existing document parsers."""

from typing import Literal

from pydantic import BaseModel

ParserFormat = Literal["pdf", "docx", "xlsx", "pptx", "txt", "csv"]


class ParserFormatInfo(BaseModel):
    file_format: ParserFormat
    extension: str
    active_parser: str
    fallback_parser: str | None
    active_available: bool
    fallback_available: bool | None


class DocumentParserInfoResponse(BaseModel):
    docling_enabled: bool
    formats: list[ParserFormatInfo]


class DocumentParserTestResponse(BaseModel):
    file_format: ParserFormat
    ok: bool
    parser_used: str
    latency_ms: int
    page_count: int | None
    chunk_count: int | None
    error: str | None
