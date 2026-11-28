"""Text-to-speech for voice replies (Step O, slice 5).

Provider decision (recorded here so it is not revisited silently):

* ``none`` (the default) — no synthesis; the caller falls back to text.
  This is the safe default: nothing is called, nothing is spent, and the
  founder still gets the reply as text.
* ``local`` — the machine's own speech synthesizer (``espeak-ng``, falling
  back to ``espeak``), run as a subprocess with the text on stdin and the
  audio written to a temporary WAV file that is read and deleted. No network,
  no key, no audio kept. When no synthesizer binary is installed the
  provider reports unavailable (``None``) instead of failing — the text
  fallback applies, exactly as if the provider were ``none``.
* ``api`` — a cloud TTS API. No TTS API is configured in this codebase
  (no provider, no key, no endpoint), so it also reports unavailable until
  an owner configures one. It never invents credentials or calls anything.

The provider is chosen by the ``voice_tts_provider`` setting. Synthesis is
best-effort by design: any failure (missing binary, nonzero exit, empty
output, timeout) returns ``None`` and the caller serves the text instead.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.voice.reply import MAX_REPLY_CHARS

PROVIDERS = ("none", "local", "api")
_LOCAL_BINARIES = ("espeak-ng", "espeak")
_LOCAL_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class TTSAudio:
    """Synthesized audio for one reply."""

    audio: bytes
    content_type: str  # "audio/wav" for the local provider
    provider: str


def synthesize(text: str, *, provider: str = "none", timeout: int = _LOCAL_TIMEOUT_SECONDS) -> TTSAudio | None:
    """Synthesize ``text`` with the named provider, or ``None`` when no audio
    can be produced. Never raises for an unavailable provider — only an
    unknown provider name is a programming error."""
    name = (provider or "none").strip().lower()
    if name == "none":
        return None
    if name == "local":
        return _synthesize_local(text, timeout=timeout)
    if name == "api":
        return None  # no TTS API configured; text fallback applies
    raise ValueError(f"unknown TTS provider: {provider!r}")


def _synthesize_local(text: str, timeout: int) -> TTSAudio | None:
    binary = shutil.which(_LOCAL_BINARIES[0]) or shutil.which(_LOCAL_BINARIES[1])
    if binary is None:
        return None
    spoken = text.strip()[:MAX_REPLY_CHARS]
    if not spoken:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="voice-tts-") as tmp:
            out = Path(tmp) / "reply.wav"
            proc = subprocess.run(
                [binary, "-w", str(out)],
                input=spoken.encode("utf-8"),
                capture_output=True,
                timeout=timeout,
            )
            if proc.returncode != 0 or not out.exists():
                return None
            data = out.read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not data:
        return None
    return TTSAudio(audio=data, content_type="audio/wav", provider="local")
