"""Offline speech transcription for audio files and video audio tracks.

The parser runs inside the existing killable ingestion child. PyAV decodes bounded frames from local files;
faster-whisper runs only against a configured on-disk model and never downloads or sends media to a provider.
Audio is converted in short temporary PCM segments so multi-hour inputs do not become one giant in-memory array.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import tempfile
import wave
from collections.abc import Iterable
from pathlib import Path

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text

SAMPLE_RATE = 16_000
_SAMPLE_WIDTH = 2  # mono s16
logger = logging.getLogger(__name__)


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _configuration(*, require_speech_model: bool = True) -> tuple[object, Path | None]:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.rag_media_enabled:
        raise ParseError("media_disabled", "Audio/video transcription is disabled by the administrator.")
    model_path = Path(settings.rag_media_model_path).expanduser() if settings.rag_media_model_path else None
    if require_speech_model and (model_path is None or not model_path.is_dir()):
        raise ParseError("media_model_unavailable", "Configure a local speech model before importing audio or video.")
    if require_speech_model and importlib.util.find_spec("faster_whisper") is None:
        raise ParseError("media_runtime_unavailable", "Install the optional local media transcription runtime.")
    if importlib.util.find_spec("av") is None:
        raise ParseError("media_runtime_unavailable", "Install the optional local media decoding runtime.")
    return settings, model_path.resolve() if model_path is not None else None


def _transcribe_wav(model, wav_path: Path, offset_seconds: float) -> Iterable[tuple[float, float, str]]:
    try:
        # VAD can discard a short, quiet, or heavily compressed voice note entirely. Retry an empty result
        # without VAD and with relaxed decoder thresholds; retain only plausible speech to limit hallucinations.
        attempts = (
            {"beam_size": 1, "vad_filter": True},
            {
                "beam_size": 5,
                "vad_filter": False,
                "no_speech_threshold": 0.9,
                "log_prob_threshold": -2.0,
                "compression_ratio_threshold": 3.0,
            },
            {
                # WhatsApp voice notes in Pakistan are often Urdu/Roman Urdu. Auto detection can
                # discard short, compressed clips; a forced language retry is used only after both
                # automatic passes produced no acceptable transcript.
                "beam_size": 5,
                "vad_filter": False,
                "language": "ur",
                "no_speech_threshold": 0.9,
                "log_prob_threshold": -2.0,
                "compression_ratio_threshold": 3.0,
            },
        )
        for options in attempts:
            segments, _info = model.transcribe(
                str(wav_path),
                condition_on_previous_text=False,
                **options,
            )
            transcript = []
            for segment in segments:
                text = " ".join(str(segment.text).split()).strip()
                if not text:
                    continue
                no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
                avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
                if no_speech > 0.9 or avg_logprob < -1.5:
                    continue
                transcript.append((
                    offset_seconds + float(segment.start),
                    offset_seconds + float(segment.end),
                    text,
                ))
            if transcript:
                yield from transcript
                return
            if options.get("language") == "ur":
                return
    except Exception:
        raise ParseError("media_transcription_failed", "Local speech transcription failed for this file.") from None


def _video_frame_text(path: Path, settings, limits: ParseLimits, av_module, budget: TextBudget) -> list[Section]:
    """OCR sparsely sampled video frames with the deployment's local RapidOCR model and bounded text budget."""
    if not settings.rag_media_video_frame_ocr_enabled:
        return []
    try:
        import numpy as np

        from app.rag.ocr import _local_engine

        engine = _local_engine()
        container = av_module.open(str(path), mode="r")
    except ParseError:
        raise
    except Exception:
        raise ParseError("video_ocr_unavailable", "Local video frame OCR could not be initialized.") from None

    sections: list[Section] = []
    try:
        streams = container.streams.video
        if not streams:
            return []
        stream = streams[0]
        duration = float(container.duration / av_module.time_base) if container.duration is not None else None
        max_frames = min(settings.rag_media_video_max_frames, limits.max_units)
        interval = float(settings.rag_media_video_frame_interval_seconds)
        step = max(interval, duration / max_frames) if duration and duration > 0 else interval
        sample_count = min(max_frames, math.ceil(duration / step)) if duration and duration > 0 else max_frames
        seen_text: set[str] = set()

        for sample in range(sample_count):
            target = sample * step
            if duration and target >= duration:
                break
            frame = None
            try:
                container.seek(
                    int(target / float(stream.time_base)),
                    stream=stream,
                    backward=True,
                    any_frame=False,
                )
                for candidate in container.decode(stream):
                    timestamp = float(candidate.time) if candidate.time is not None else target
                    if timestamp + 0.001 >= target:
                        frame = candidate
                        break
            except Exception:
                raise ParseError("video_decode_failed", "A video frame could not be decoded.") from None
            if frame is None:
                continue

            try:
                timestamp = float(frame.time) if frame.time is not None else target
                width, height = int(frame.width), int(frame.height)
                if max(width, height) > 1280:
                    scale = 1280 / max(width, height)
                    frame = frame.reformat(
                        width=max(1, round(width * scale)),
                        height=max(1, round(height * scale)),
                        format="rgb24",
                    )
                pixels = frame.to_ndarray(format="rgb24")
                result = engine(np.ascontiguousarray(pixels))
                text = " ".join(part.strip() for part in (getattr(result, "txts", None) or ()) if part.strip())
            except Exception:
                raise ParseError("video_ocr_failed", "Local OCR could not read a sampled video frame.") from None
            key = " ".join(text.casefold().split())
            if not key or key in seen_text:
                continue
            remaining = limits.max_chars - budget.used
            if remaining <= 0:
                break
            text = budget.take(text[:remaining])
            if not text:
                break
            seen_text.add(key)
            label = f"Frame at {_timestamp(timestamp)}"
            sections.append(
                Section(
                    f"On-screen text at {_timestamp(timestamp)}: {text}",
                    Locator(
                        "video_frame",
                        len(sections) + 1,
                        label,
                        {"timestamp_seconds": round(timestamp, 3)},
                    ),
                )
            )
    finally:
        container.close()
    return sections


class _MediaParser:
    file_format = ""
    extensions: tuple[str, ...] = ()

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        # Video may contain usable visual information without any audio track; do not require
        # Whisper weights until the decoder confirms that speech transcription is needed.
        settings, _model_path = _configuration(require_speech_model=self.file_format != "video")
        if path.stat().st_size > settings.rag_media_max_file_bytes:
            raise ParseError(
                "file_too_large",
                f"The media file exceeds the {settings.rag_media_max_file_mb} MB media limit.",
            )
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        return self._parse(path, limits)

    def _parse(
        self,
        path: Path,
        limits: ParseLimits,
        *,
        max_duration_seconds: int | None = None,
    ) -> list[Section]:
        settings, model_path = _configuration(require_speech_model=False)
        if path.stat().st_size > getattr(settings, "rag_media_max_file_bytes", 8192 * 1024 * 1024):
            raise ParseError("file_too_large", "The media file exceeds the configured processing limit.")
        duration_limit = getattr(settings, "rag_media_max_duration_seconds", 14_400)
        if max_duration_seconds is not None:
            duration_limit = min(duration_limit, max_duration_seconds)
        try:
            import av
        except ImportError:
            raise ParseError("media_runtime_unavailable", "Install the optional local media transcription runtime.") from None

        try:
            container = av.open(str(path), mode="r")
        except Exception:
            raise ParseError("invalid_media", "The audio/video file could not be opened.") from None

        try:
            audio_streams = container.streams.audio
            video_streams = container.streams.video if self.file_format == "video" else []
            if not audio_streams and not video_streams:
                raise ParseError("no_audio_stream", "The media file contains no supported audio or video stream.")
            stream = audio_streams[0] if audio_streams else None
            declared_duration = (
                float(container.duration / av.time_base)
                if container.duration is not None
                else None
            )
            if declared_duration is not None and declared_duration > duration_limit:
                raise ParseError("media_too_long", "The media duration exceeds the configured limit.")
            model = None
            if stream is not None:
                settings, model_path = _configuration(require_speech_model=True)
                from faster_whisper import WhisperModel

                model = WhisperModel(
                    str(model_path),
                    device=settings.rag_media_device,
                    compute_type="int8" if settings.rag_media_device == "cpu" else "float16",
                    local_files_only=True,
                    cpu_threads=settings.rag_media_cpu_threads,
                )
        except ParseError as exc:
            if self.file_format == "video" and exc.code in {"media_model_unavailable", "media_runtime_unavailable"}:
                # Video frames may still contain useful text when local speech weights are not staged.
                logger.warning("Skipping video audio transcription (%s)", exc.code)
                stream, model = None, None
            else:
                try:
                    container.close()
                except Exception:
                    pass
                raise
        except ImportError:
            if self.file_format == "video":
                logger.warning("Skipping video audio transcription because the local speech runtime is unavailable")
                stream, model = None, None
            else:
                try:
                    container.close()
                except Exception:
                    pass
                raise ParseError("media_runtime_unavailable", "Install the optional local media transcription runtime.") from None
        except Exception:
            if self.file_format == "video":
                logger.warning("Skipping video audio transcription because the local speech model could not be loaded")
                stream, model = None, None
            else:
                try:
                    container.close()
                except Exception:
                    pass
                raise ParseError("media_model_unavailable", "The configured local speech model could not be loaded.") from None

        budget = TextBudget(limits.max_chars)
        sections: list[Section] = []
        current: list[str] = []
        current_chars = 0
        start_seconds = end_seconds = 0.0

        def flush() -> None:
            nonlocal current, current_chars
            if not current:
                return
            position = len(sections) + 1
            label = f"{_timestamp(start_seconds)}–{_timestamp(end_seconds)}"
            sections.append(
                Section(
                    " ".join(current),
                    Locator(
                        "time", position, label,
                        {"start_seconds": round(start_seconds, 3), "end_seconds": round(end_seconds, 3)},
                    ),
                )
            )
            current, current_chars = [], 0

        def append_transcript(start: float, end: float, text: str) -> None:
            nonlocal current_chars, start_seconds, end_seconds
            text = budget.take(text)
            if current and current_chars + len(text) + 1 > limits.section_chars:
                flush()
            if not current:
                start_seconds = start
            current.append(text)
            current_chars += len(text) + 1
            end_seconds = max(end, start)

        try:
            if stream is not None:
                resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
                segment_target = settings.rag_media_segment_seconds * SAMPLE_RATE
                max_samples = duration_limit * SAMPLE_RATE
                pcm_blocks: list[bytes] = []
                segment_samples = total_samples = 0

            def consume_resampled(frames) -> None:
                nonlocal pcm_blocks, segment_samples, total_samples
                for converted in frames or ():
                    sample_count = int(converted.samples)
                    if sample_count <= 0:
                        continue
                    if total_samples + sample_count > max_samples:
                        raise ParseError("media_too_long", "The media duration exceeds the configured limit.")
                    pcm_blocks.append(converted.to_ndarray().tobytes())
                    segment_samples += sample_count
                    total_samples += sample_count
                    if segment_samples >= segment_target:
                        transcribe_segment()

            def transcribe_segment() -> None:
                nonlocal pcm_blocks, segment_samples
                if segment_samples == 0:
                    return
                segment_start = (total_samples - segment_samples) / SAMPLE_RATE
                with tempfile.TemporaryDirectory(prefix="infraspine-media-") as directory:
                    wav_path = Path(directory) / "segment.wav"
                    with wave.open(str(wav_path), "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(_SAMPLE_WIDTH)
                        output.setframerate(SAMPLE_RATE)
                        output.writeframes(b"".join(pcm_blocks))
                    for start, end, text in _transcribe_wav(model, wav_path, segment_start):
                        append_transcript(start, end, text)
                pcm_blocks, segment_samples = [], 0

            if stream is not None:
                try:
                    for frame in container.decode(stream):
                        consume_resampled(resampler.resample(frame))
                    consume_resampled(resampler.resample(None))
                    transcribe_segment()
                except ParseError as exc:
                    if self.file_format != "video" or exc.code == "media_too_long":
                        raise
                    logger.warning("Video audio transcription failed (%s); continuing with frame OCR", exc.code)
                except Exception:
                    if self.file_format != "video":
                        raise ParseError("invalid_media", "The audio/video stream could not be decoded.") from None
                    logger.warning("Video audio decoding failed; continuing with frame OCR")
            flush()
        finally:
            container.close()
        if self.file_format == "video":
            try:
                sections.extend(_video_frame_text(path, settings, limits, av, budget))
            except ParseError as exc:
                if not sections:
                    raise
                logger.warning("Video frame OCR failed (%s); retaining audio transcript", exc.code)
            sections.sort(
                key=lambda section: float(
                    section.locator.attrs.get("start_seconds", section.locator.attrs.get("timestamp_seconds", 0))
                )
            )
            sections = [
                Section(
                    section.text,
                    Locator(section.locator.kind, position, section.locator.label, section.locator.attrs),
                )
                for position, section in enumerate(sections, start=1)
            ]
        return require_text(sections, " Speech could not be recognized in this file.")


class AudioParser(_MediaParser):
    file_format = "audio"
    # WebM stays mapped to VideoParser in the general registry, but its audio stream is valid
    # for the dedicated spoken-question route used by browser MediaRecorder.
    extensions = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".webm")

    def parse_spoken_question(
        self, path: Path, limits: ParseLimits, *, max_duration_seconds: int
    ) -> list[Section]:
        """Transcribe a short, unpersisted voice question within its route-specific duration cap."""
        if max_duration_seconds < 1:
            raise ValueError("max_duration_seconds must be positive")
        return self._parse(path, limits, max_duration_seconds=max_duration_seconds)


class VideoParser(_MediaParser):
    file_format = "video"
    extensions = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
