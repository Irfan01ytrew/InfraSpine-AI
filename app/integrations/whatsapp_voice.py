"""WhatsApp voice notes: bounded media download + local Whisper first, API fallback.

Cloud audio messages arrive with a Meta media id sealed in the inbound row;
Baileys voice notes arrive as base64 audio from the bridge. Both are staged
to a temporary file, transcribed, and the staged audio is removed before the
helper returns (same rule as the voice-question upload). Voice replies stay
text: only the transcript is fed to the agent, marked as machine text.

Fallback rule (same as the rest of the platform): local Whisper first; the
API transcriber is tried only when local transcription is *unavailable*
(model not configured, runtime missing, transcription disabled) and only
through the provider named by ``llm_fallback_provider``. An unintelligible
recording is never sent to the API. When no transcriber is available the
caller replies "[NEEDS INPUT]"; nothing is ever retried blindly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from app.core.errors import FileRejectedError, FileTooLargeError, InvalidInputError
from app.integrations.transport import TransportError, request as provider_request
from app.providers.endpoint_security import (
    EndpointPolicy,
    UnsafeEndpointError,
    guarded_http_client,
    normalize_base_url,
)
from app.rag.parsers.base import ParseError
from app.rag.parsers.media_parser import AudioParser
from app.rag.voice_questions import (
    MAX_VOICE_QUESTION_CHARS,
    transcribe_spoken_question,
)

MAX_WHATSAPP_VOICE_BYTES = 8 * 1024 * 1024
# Base64 of the largest accepted audio file (4 chars per 3 bytes, padded).
MAX_WHATSAPP_VOICE_B64_CHARS = 4 * ((MAX_WHATSAPP_VOICE_BYTES + 2) // 3)
VOICE_TRANSCRIPT_PREFIX = "[Voice note transcript] "
VOICE_DOWNLOAD_TIMEOUT_SECONDS = 20

# WhatsApp voice notes are Opus in OGG; other audio the platform can decode is accepted too.
MIME_TO_SUFFIX = {
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
}

# Meta serves WhatsApp media from its own CDN (or the Graph API host). A media
# URL pointing anywhere else is refused even though it came from Meta's API.
_MEDIA_HOSTS = ("graph.facebook.com",)
_MEDIA_HOST_SUFFIXES = (".fbcdn.net",)

# Local transcription failures that mean "no transcriber is available" (fall
# back to the API). Any other ParseError means the audio itself is the
# problem, and the API is not tried.
LOCAL_UNAVAILABLE_CODES = {"media_disabled", "media_model_unavailable", "media_runtime_unavailable"}

TRANSCRIPTION_UNAVAILABLE = "Voice transcription is unavailable right now. Please resend your request as text."


def suffix_for_mime(mime_type: str | None) -> str | None:
    """Map a WhatsApp audio MIME type to a decodable file suffix, or None."""
    if not isinstance(mime_type, str):
        return None
    mime = mime_type.split(";", 1)[0].strip().lower()
    suffix = MIME_TO_SUFFIX.get(mime)
    return suffix if suffix in AudioParser.extensions else None


def mime_for_suffix(suffix: str) -> str:
    """First MIME type for a suffix (used for the API transcription upload)."""
    for mime, mapped in MIME_TO_SUFFIX.items():
        if mapped == suffix:
            return mime
    return "audio/ogg"


def _check_audio_bytes(data: bytes) -> None:
    if len(data) > MAX_WHATSAPP_VOICE_BYTES:
        raise FileTooLargeError("The voice note exceeds the configured audio size limit.")
    if not data:
        raise FileRejectedError("The voice note is empty.")


async def _download_bytes(url: str) -> bytes:
    """Stream a Meta media URL to memory, bounded, through the guarded client.

    The URL's origin is pinned by the guarded client (DNS-checked, no
    redirects); the host was already allow-listed by the caller.
    """
    try:
        endpoint = normalize_base_url(url)
    except UnsafeEndpointError as exc:
        raise TransportError("provider_rejected") from exc
    policy = EndpointPolicy()
    try:
        async with asyncio.timeout(VOICE_DOWNLOAD_TIMEOUT_SECONDS):
            async with guarded_http_client(endpoint, policy) as client:
                async with client.stream("GET", url, timeout=VOICE_DOWNLOAD_TIMEOUT_SECONDS) as response:
                    status = response.status_code
                    if status == 429:
                        raise TransportError("rate_limited", retryable=True)
                    if status in (401, 403):
                        raise TransportError("credentials_or_scope_rejected")
                    if status >= 500:
                        raise TransportError("provider_unavailable", retryable=True)
                    if status != 200:
                        raise TransportError("provider_rejected")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_WHATSAPP_VOICE_BYTES:
                            raise FileTooLargeError("The voice note exceeds the configured audio size limit.")
                        raw.extend(chunk)
                    return bytes(raw)
    except (TransportError, FileTooLargeError):
        raise
    except UnsafeEndpointError as exc:
        raise TransportError("provider_rejected") from exc
    except Exception as exc:
        raise TransportError("transport_unavailable", retryable=True) from exc


async def download_cloud_audio(
    media_id: str,
    *,
    api_version: str,
    access_token: str,
    meta_request: Callable[..., Awaitable] | None = None,
    fetch_bytes: Callable[[str], Awaitable[bytes]] | None = None,
) -> tuple[bytes, str]:
    """Download a Cloud audio message: Meta media metadata first, then bytes.

    Returns (audio bytes, file suffix). The declared file size is checked
    before any bytes are fetched; the stream itself is capped too. Raises
    FileTooLargeError / FileRejectedError / TransportError; the caller maps
    all of them to a "[NEEDS INPUT]" reply.

    The transport callables resolve at call time (not as def-time defaults)
    so tests can substitute them by patching this module's attributes.
    """
    if not isinstance(media_id, str) or not 1 <= len(media_id) <= 160:
        raise FileRejectedError("The voice note has no usable media reference.")
    meta = meta_request or provider_request
    fetch = fetch_bytes or _download_bytes
    response = await meta(
        "https://graph.facebook.com/" + api_version,
        "/" + media_id,
        token=access_token,
    )
    body = response.body
    if not isinstance(body, dict):
        raise TransportError("invalid_provider_response")
    url = body.get("url")
    if not isinstance(url, str) or not url:
        raise TransportError("invalid_provider_response")
    host = (urlparse(url).hostname or "").lower()
    if host not in _MEDIA_HOSTS and not any(host.endswith(suffix) for suffix in _MEDIA_HOST_SUFFIXES):
        raise TransportError("provider_rejected")
    declared_size = body.get("file_size")
    if isinstance(declared_size, int) and declared_size > MAX_WHATSAPP_VOICE_BYTES:
        raise FileTooLargeError("The voice note exceeds the configured audio size limit.")
    suffix = suffix_for_mime(body.get("mime_type"))
    if suffix is None:
        raise FileRejectedError("The voice note is in an unsupported audio format.")
    data = await fetch(url)
    _check_audio_bytes(data)
    return data, suffix


def decode_baileys_audio(encoded: str | None, mime_type: str | None) -> tuple[bytes, str]:
    """Decode a bridge voice note (base64 audio + MIME type) within the size bound."""
    if not isinstance(encoded, str) or not encoded:
        raise FileRejectedError("The voice note has no audio data.")
    if len(encoded) > MAX_WHATSAPP_VOICE_B64_CHARS:
        raise FileTooLargeError("The voice note exceeds the configured audio size limit.")
    suffix = suffix_for_mime(mime_type)
    if suffix is None:
        raise FileRejectedError("The voice note is in an unsupported audio format.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FileRejectedError("The voice note could not be decoded.") from exc
    _check_audio_bytes(data)
    return data, suffix


async def _transcribe_with_api(
    audio_path: Path,
    suffix: str,
    *,
    runtime=None,
    db=None,
    api_transcriber: Callable[[Path], Awaitable[str]] | None = None,
) -> str:
    """Transcribe via the configured fallback provider, or raise InvalidInputError."""
    if api_transcriber is not None:
        text = await api_transcriber(audio_path)
        return str(text or "").strip()
    if runtime is None or db is None:
        raise InvalidInputError(TRANSCRIPTION_UNAVAILABLE)
    try:
        client = await runtime.get_audio_transcription_client(db)
        text = await client.transcribe_audio(
            audio_path.read_bytes(),
            filename=f"voice{suffix}",
            mime_type=mime_for_suffix(suffix),
        )
    except Exception:
        # Only the failure is reported: provider error bodies can echo key fragments.
        raise InvalidInputError(TRANSCRIPTION_UNAVAILABLE) from None
    return str(text or "").strip()


async def transcribe_voice_note(
    audio_bytes: bytes,
    suffix: str,
    *,
    runtime=None,
    db=None,
    transcriber: Callable[[Path], str] | None = None,
    api_transcriber: Callable[[Path], Awaitable[str]] | None = None,
    run_in_thread: Callable[..., Awaitable[str]] = asyncio.to_thread,
    temporary_directory: Callable[..., tempfile.TemporaryDirectory] = tempfile.TemporaryDirectory,
) -> tuple[str, str]:
    """Transcribe staged audio and remove it before returning.

    Returns (transcript, source) where source is "local" or "api". The local
    Whisper model runs first; the API fallback is used only when local
    transcription is unavailable (see LOCAL_UNAVAILABLE_CODES). Raises
    FileTooLargeError / FileRejectedError / InvalidInputError.
    """
    if suffix not in AudioParser.extensions:
        raise FileRejectedError("The voice note is in an unsupported audio format.")
    _check_audio_bytes(audio_bytes)
    parse = transcriber or transcribe_spoken_question
    with temporary_directory(prefix="infraspine-whatsapp-voice-") as directory:
        audio_path = Path(directory) / f"voice{suffix}"
        audio_path.write_bytes(audio_bytes)
        try:
            transcript = await run_in_thread(parse, audio_path)
            source = "local"
        except ParseError as exc:
            if exc.code not in LOCAL_UNAVAILABLE_CODES:
                raise FileRejectedError(exc.message) from None
            transcript = await _transcribe_with_api(
                audio_path, suffix, runtime=runtime, db=db, api_transcriber=api_transcriber
            )
            source = "api"
    transcript = transcript[:MAX_VOICE_QUESTION_CHARS].strip()
    if not transcript:
        raise FileRejectedError("No speech could be recognized in this recording.")
    return transcript, source


def mark_machine_text(transcript: str) -> str:
    """Mark a transcript as machine text so the agent never mistakes it for a typed message."""
    return VOICE_TRANSCRIPT_PREFIX + transcript.strip()


def strip_machine_text_prefix(text: str) -> str:
    """Remove the voice-note transcript marker, for command parsing only.

    The marked transcript stays the task text; only the decision check
    looks at the unmarked form, so a spoken "APPROVE …" is a decision
    (Step F slice 6 — slice 5 recorded that it never parsed). Anything
    that is not a command still runs as the marked transcript.
    """
    if isinstance(text, str) and text.startswith(VOICE_TRANSCRIPT_PREFIX):
        return text[len(VOICE_TRANSCRIPT_PREFIX):]
    return text
