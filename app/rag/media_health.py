"""Admin-safe status for optional local speech transcription."""

import importlib.util
from pathlib import Path


def media_status(settings) -> dict:
    try:
        runtime = importlib.util.find_spec("faster_whisper") is not None and importlib.util.find_spec("av") is not None
        video_ocr = importlib.util.find_spec("rapidocr") is not None and importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        runtime = False
        video_ocr = False
    model = bool(settings.rag_media_model_path and Path(settings.rag_media_model_path).expanduser().is_dir())
    if not settings.rag_media_enabled:
        state = "disabled"
    elif runtime and model:
        state = "ready"
    else:
        state = "unavailable"
    return {
        "enabled": settings.rag_media_enabled,
        "runtime_available": runtime,
        "model_available": model,
        "status": state,
        "device": settings.rag_media_device,
        "max_file_mb": settings.rag_media_max_file_mb,
        "max_duration_seconds": settings.rag_media_max_duration_seconds,
        "segment_seconds": settings.rag_media_segment_seconds,
        "video_frame_ocr_enabled": settings.rag_media_video_frame_ocr_enabled,
        "video_frame_ocr_available": video_ocr,
        "video_frame_interval_seconds": settings.rag_media_video_frame_interval_seconds,
        "video_max_frames": settings.rag_media_video_max_frames,
    }
