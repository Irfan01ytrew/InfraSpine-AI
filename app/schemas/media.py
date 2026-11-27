"""Media transcription capability status."""

from typing import Literal

from pydantic import BaseModel


class MediaTranscriptionStatus(BaseModel):
    enabled: bool
    runtime_available: bool
    model_available: bool
    status: Literal["ready", "disabled", "unavailable"]
    device: Literal["cpu", "cuda"]
    max_file_mb: int
    max_duration_seconds: int
    segment_seconds: int
    video_frame_ocr_enabled: bool
    video_frame_ocr_available: bool
    video_frame_interval_seconds: int
    video_max_frames: int
