"""Admin-only model status and health-check responses."""

from typing import Literal

from pydantic import BaseModel


class EmbeddingModelStatusResponse(BaseModel):
    model_name: str
    dimensions: int
    device: Literal["cpu"] = "cpu"
    available_locally: bool
    loaded: bool
    status: Literal["ready", "not_loaded", "unavailable"]


class EmbeddingModelTestResponse(BaseModel):
    ok: bool
    model_name: str
    dimensions: int
    latency_ms: int
    message: str
