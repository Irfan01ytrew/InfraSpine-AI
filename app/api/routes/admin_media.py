"""Administrator-only configuration status for offline speech transcription."""

from fastapi import APIRouter, Depends

from app.api.deps import AppSettings, require_admin
from app.rag.media_health import media_status
from app.schemas.media import MediaTranscriptionStatus

router = APIRouter(
    prefix="/api/admin/media-transcription",
    tags=["admin-media"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=MediaTranscriptionStatus)
async def status(settings: AppSettings) -> MediaTranscriptionStatus:
    return MediaTranscriptionStatus(**media_status(settings))
