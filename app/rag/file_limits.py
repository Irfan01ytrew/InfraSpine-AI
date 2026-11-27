"""Per-record size caps for direct uploads versus resumable, administrator-controlled imports."""

from app.core.config import Settings


def max_ingested_bytes(settings: Settings, file_format: str, *, imported: bool = False) -> int:
    if file_format in {"audio", "video"}:
        return settings.rag_media_max_file_bytes
    if imported:
        return settings.rag_import_max_file_bytes
    return settings.rag_max_upload_bytes
