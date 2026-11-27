"""Bounded, on-device image generation used when a cloud image endpoint is unavailable.

The model is loaded lazily so text chat does not pay the memory cost. Diffusion work runs in a worker
thread and is serialized per process because a single CPU model instance is shared by requests.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import LLMNotConfiguredError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError

logger = logging.getLogger(__name__)

_MODEL_ID = "segmind/tiny-sd"
_PIPELINE_LOCK = threading.RLock()
_PIPELINE: Any | None = None
_PIPELINE_PATH: Path | None = None

_ROMAN_URDU_IMAGE = re.compile(
    r"^\s*(?:(?:please|mujhe|mujai|mujhy|mere\s+liye)\s+)?"
    r"(?P<subject>.+?)\s+(?:ki|ka|ke)\s+"
    r"(?:image|iamge|imgae|imge|img|pic|tasveer|tasvir|tasweer|picture|photo)"
    r"(?:\s+(?:bna|bana|banao|bnao|draw|create|generate)(?:\s+(?:kar|kr|ke)\s+do)?)?"
    r"(?:\s+(?:ke\s+liye|do|karo|kr\s+do))?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_ROMAN_URDU_IMAGE_PREFIX = re.compile(
    r"^\s*(?:(?:please|sahi|shi|acha|achi|zara|proper|mujhe|mujai|mujhy|mere\s+liye)\s+)*"
    r"(?:images?|iamge|imgae|imge|img|pics?|tasveer|tasvir|tasweer|pictures?|photos?)\s+"
    r"(?:bnao|banao|bna|bana|draw|create|generate)"
    r"(?:\s+(?:kar|kr|ke)(?:\s+do)?)?\s+"
    r"(?P<subject>.+?)(?:\s+(?:ki|ka|ke))?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_ROMAN_URDU_LEADING = re.compile(r"^(?:(?:mujhe|mujai|mujhy|mere\s+liye)\s+)+", re.IGNORECASE)
_IMAGE_REQUEST_FILLER = re.compile(
    r"^\s*(?:(?:please|sahi|shi|acha|achi|zara|proper|mujhe|mujai|mujhy|mere\s+liye)\s+)+",
    re.IGNORECASE,
)
_ENGLISH_IMAGE_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:create|generate|draw|make|paint|illustrate)\s+"
    r"(?:an?\s+)?(?:image|picture|photo|portrait|illustration|artwork)\s+(?:of\s+)?(?P<subject>.+?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_SUBJECT_FIRST_IMAGE_REQUEST = re.compile(
    r"^\s*(?P<subject>.+?)\s+(?:ki|ka|ke)\s+"
    r"(?:image|iamge|imgae|imge|img|pic|pics|picture|pictures|photo|photos|tasveer|tasvir|tasweer)"
    r"(?:\s+(?:bna|bana|banao|bnao|draw|create|generate)(?:\s+(?:kar|kr|ke)\s+do)?)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def local_image_model_ready(settings: Settings) -> bool:
    """Return true only when a complete, already-downloaded local model is configured."""
    configured = settings.rag_local_image_model_path
    if configured is None:
        return False
    model_dir = Path(configured).expanduser()
    required = (
        model_dir / "model_index.json",
        model_dir / "text_encoder" / "pytorch_model.bin",
        model_dir / "unet" / "diffusion_pytorch_model.bin",
        model_dir / "vae" / "diffusion_pytorch_model.bin",
    )
    return all(path.is_file() and path.stat().st_size > 0 for path in required)


def normalize_image_prompt(prompt: str) -> str:
    """Turn common English/Roman Urdu commands into a descriptive, model-ready image prompt."""
    request = prompt.strip()[:4_000]
    cleaned = _IMAGE_REQUEST_FILLER.sub("", request)
    match = (
        _ROMAN_URDU_IMAGE.fullmatch(cleaned)
        or _ROMAN_URDU_IMAGE_PREFIX.fullmatch(cleaned)
        or _SUBJECT_FIRST_IMAGE_REQUEST.fullmatch(cleaned)
        or _ENGLISH_IMAGE_REQUEST.fullmatch(cleaned)
    )
    if match:
        subject = _ROMAN_URDU_LEADING.sub("", match.group("subject").strip(" ,.:;!?"))
        subject = re.sub(r"\bimran\s+kahn\b", "Imran Khan", subject, flags=re.IGNORECASE)
        subject = re.sub(r"\s+(?:ki|ka|ke)$", "", subject, flags=re.IGNORECASE).strip()
        if subject:
            if re.search(r"\bimran\s+khan\b", subject, re.IGNORECASE):
                return (
                    "A respectful, recognizable editorial portrait of Imran Khan, the Pakistani public figure, "
                    "former prime minister and former international cricketer. Depict a mature Pakistani man, "
                    "dark hair, composed expression, wearing a dark traditional waistcoat over a light shalwar kameez. "
                    "Keep the face masculine and recognizable, natural proportions, detailed eyes, realistic skin, "
                    "professional portrait photography, clean background, no text, no watermark."
                )
            return (
                f"Create a polished, high-quality image of {subject}. Follow the subject and requested scene closely; "
                "use a balanced composition, natural proportions, clear details, and professional lighting."
            )
    return request


def _model_prompt(prompt: str) -> str:
    """Compatibility alias for older callers."""
    return normalize_image_prompt(prompt)


def _load_pipeline(model_path: Path, cpu_threads: int):
    global _PIPELINE, _PIPELINE_PATH
    if _PIPELINE is not None and _PIPELINE_PATH == model_path:
        return _PIPELINE

    import torch
    from diffusers import StableDiffusionPipeline

    torch.set_num_threads(cpu_threads)
    pipeline = StableDiffusionPipeline.from_pretrained(
        str(model_path),
        torch_dtype=torch.float32,
        local_files_only=True,
        safety_checker=None,
        requires_safety_checker=False,
    )
    pipeline.set_progress_bar_config(disable=True)
    pipeline.to("cpu")
    _PIPELINE = pipeline
    _PIPELINE_PATH = model_path
    return pipeline


def _generate_sync(prompt: str, model_path: Path, steps: int, size: int, cpu_threads: int) -> bytes:
    with _PIPELINE_LOCK:
        pipeline = _load_pipeline(model_path, cpu_threads)
        import torch

        generator = torch.Generator(device="cpu").manual_seed(secrets.randbits(63))
        with torch.inference_mode():
            image = pipeline(
                _model_prompt(prompt),
                num_inference_steps=steps,
                guidance_scale=7.5,
                height=size,
                width=size,
                negative_prompt="low quality, blurry, deformed, unreadable text, watermark, signature",
                generator=generator,
            ).images[0]
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG", optimize=False)
        return output.getvalue()


async def generate_local_image(settings: Settings, prompt: str) -> tuple[bytes, str, str, str]:
    """Generate a 512px-or-smaller PNG without blocking the ASGI event loop."""
    if not local_image_model_ready(settings):
        raise LLMUnavailableError("The configured local image model is missing or incomplete.")
    model_path = Path(settings.rag_local_image_model_path).expanduser().resolve()
    try:
        image = await asyncio.to_thread(
            _generate_sync,
            prompt,
            model_path,
            settings.rag_local_image_steps,
            settings.rag_local_image_size,
            settings.rag_local_image_cpu_threads,
        )
    except (LLMNotConfiguredError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError):
        raise
    except Exception as exc:
        logger.exception("Local image generation failed (model=%s)", _MODEL_ID)
        raise LLMUnavailableError("The local image model failed to generate an image.") from exc
    if not image:
        raise LLMUnavailableError("The local image model returned an empty image.")
    logger.info("Generated image locally with %s (%s bytes)", _MODEL_ID, len(image))
    return image, "image/png", _MODEL_ID, "local-diffusers"


async def generate_image_with_local_fallback(
    settings: Settings,
    prompt: str,
    client_factory,
) -> tuple[bytes, str, str, str]:
    """Try the selected cloud image provider, then the configured private local model."""
    provider_error: Exception | None = None
    try:
        client = await client_factory()
    except (LLMNotConfiguredError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError) as exc:
        client = None
        provider_error = exc

    if client is not None and bool(getattr(client, "supports_image_generation", False)):
        try:
            return await client.generate_image(prompt)
        except (LLMNotConfiguredError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError, ValueError) as exc:
            provider_error = exc
            logger.info("Cloud image generation failed; trying local model (%s)", type(exc).__name__)

    if local_image_model_ready(settings):
        return await generate_local_image(settings, prompt)
    if provider_error is not None:
        raise provider_error
    raise LLMUnavailableError("No configured image provider or local image model is available.")
