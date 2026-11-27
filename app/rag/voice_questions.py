"""Bounded local transcription helpers for temporary spoken RAG questions."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from app.core.errors import FileRejectedError, FileTooLargeError, InvalidInputError
from app.core.config import get_settings
from app.rag.parsers.base import ParseError, ParseLimits
from app.rag.parsers.media_parser import AudioParser

MAX_VOICE_QUESTION_BYTES = 8 * 1024 * 1024
MAX_VOICE_QUESTION_SECONDS = 120
MAX_VOICE_QUESTION_CHARS = 4000
VOICE_QUESTION_SECTION_CHARS = 1000


class AudioUpload(Protocol):
    filename: str | None
    size: int | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


def supported_audio_suffix(filename: str | None) -> str | None:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in AudioParser.extensions else None


def voice_question_file_limit() -> int:
    """Never allow a voice question to exceed either the small route cap or media policy."""
    return min(MAX_VOICE_QUESTION_BYTES, get_settings().rag_media_max_file_bytes)


def transcribe_spoken_question(path: Path) -> str:
    """Use the configured on-disk Whisper model and return a short transcript without storing it."""
    limits = ParseLimits(
        max_units=1,
        max_chars=MAX_VOICE_QUESTION_CHARS,
        max_unpacked_bytes=0,
        section_chars=VOICE_QUESTION_SECTION_CHARS,
    )
    sections = AudioParser().parse_spoken_question(
        path,
        limits,
        max_duration_seconds=MAX_VOICE_QUESTION_SECONDS,
    )
    transcript = " ".join(section.text.strip() for section in sections if section.text.strip()).strip()
    if not transcript:
        raise ParseError("no_text", "No speech could be recognized in this recording.")
    return transcript[:MAX_VOICE_QUESTION_CHARS].strip()


async def transcribe_voice_upload(
    audio: AudioUpload,
    *,
    max_bytes: int,
    transcriber: Callable[[Path], str] | None = None,
    temporary_directory: Callable[..., tempfile.TemporaryDirectory] = tempfile.TemporaryDirectory,
    run_in_thread: Callable[..., Awaitable[str]] = asyncio.to_thread,
) -> tuple[str, str, int]:
    """Stage a bounded audio upload, transcribe it locally, and discard all staged audio."""
    suffix = supported_audio_suffix(audio.filename)
    if suffix is None:
        await audio.close()
        raise FileRejectedError("Choose a supported audio file such as WAV, MP3, M4A, FLAC, OGG, OPUS, or AAC.")
    if audio.size is not None and audio.size > max_bytes:
        await audio.close()
        raise FileTooLargeError("The voice question exceeds the configured audio size limit.")

    byte_count = 0
    parse = transcriber or transcribe_spoken_question
    try:
        with temporary_directory(prefix="infraspine-voice-question-") as directory:
            audio_path = Path(directory) / f"question{suffix}"
            with audio_path.open("xb") as staged:
                while chunk := await audio.read(64 * 1024):
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise FileTooLargeError("The voice question exceeds the configured audio size limit.")
                    staged.write(chunk)
            if byte_count == 0:
                raise FileRejectedError("The audio file is empty.")
            try:
                transcript = await run_in_thread(parse, audio_path)
            except ParseError as exc:
                if exc.code in {"media_disabled", "media_model_unavailable", "media_runtime_unavailable"}:
                    raise InvalidInputError(exc.message) from None
                raise FileRejectedError(exc.message) from None
    finally:
        await audio.close()

    transcript = transcript[:MAX_VOICE_QUESTION_CHARS].strip()
    if not transcript:
        raise FileRejectedError("No speech could be recognized in this recording.")
    return transcript, suffix, byte_count
