"""Read-only status and local health checks for the active embedding model."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_admin
from app.rag.embeddings import Embedder
from app.schemas.embeddings import EmbeddingModelStatusResponse, EmbeddingModelTestResponse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/embedding-models",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def _status(embedder: Embedder) -> EmbeddingModelStatusResponse:
    available = embedder.available_locally
    loaded = embedder.is_loaded
    state = "ready" if loaded else "not_loaded" if available else "unavailable"
    return EmbeddingModelStatusResponse(
        model_name=embedder.model_name,
        dimensions=embedder.dimension,
        available_locally=available,
        loaded=loaded,
        status=state,
    )


@router.get("", response_model=EmbeddingModelStatusResponse)
async def embedding_model_status(request: Request) -> EmbeddingModelStatusResponse:
    return _status(request.app.state.rag.embedder)


@router.post("/test", response_model=EmbeddingModelTestResponse)
async def test_embedding_model(request: Request) -> EmbeddingModelTestResponse:
    embedder: Embedder = request.app.state.rag.embedder
    started = time.perf_counter()
    try:
        await asyncio.to_thread(embedder.health_check)
    except Exception as exc:
        logger.warning("Embedding health check failed: %s", type(exc).__name__)
        unavailable = not embedder.is_loaded and not embedder.available_locally
        message = (
            "Model files are not available locally; the health check did not download them."
            if unavailable
            else "The embedding health check failed."
        )
        return EmbeddingModelTestResponse(
            ok=False,
            model_name=embedder.model_name,
            dimensions=embedder.dimension,
            latency_ms=round((time.perf_counter() - started) * 1000),
            message=message,
        )
    return EmbeddingModelTestResponse(
        ok=True,
        model_name=embedder.model_name,
        dimensions=embedder.dimension,
        latency_ms=round((time.perf_counter() - started) * 1000),
        message="The embedding health check passed.",
    )
