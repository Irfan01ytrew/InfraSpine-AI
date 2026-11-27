"""Authenticated download endpoint for short-lived files created in chat."""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse

from app.api.deps import AppSettings, CurrentUser
from app.services.chat_exports import MEDIA_TYPES, resolve_chat_export
from app.services.chat_images import resolve_chat_image

router = APIRouter(prefix="/api/exports", tags=["chat-exports"])
image_router = APIRouter(prefix="/api/chat-images", tags=["chat-images"])


@router.get("/download/{file_id}")
async def download_chat_export(
    file_id: Annotated[str, Path(min_length=1, max_length=150)],
    user: CurrentUser,
    settings: AppSettings,
    format: Annotated[Literal["pptx", "docx", "xlsx", "pdf"], Query()],
) -> FileResponse:
    path = resolve_chat_export(file_id, format, settings.rag_data_dir / "exports", user.id, settings.master_key_bytes)
    return FileResponse(
        path,
        media_type=MEDIA_TYPES[format],
        filename=f"infraspine-chat-response.{format}",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@image_router.get("/{token}")
async def chat_image(
    token: Annotated[str, Path(min_length=1, max_length=140)],
    user: CurrentUser,
    settings: AppSettings,
    download: Annotated[bool, Query()] = False,
) -> FileResponse:
    path = resolve_chat_image(token, settings.rag_data_dir / "generated_images", user.id, settings.master_key_bytes)
    return FileResponse(
        path,
        media_type="image/png",
        filename="infraspine-generated-image.png" if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
