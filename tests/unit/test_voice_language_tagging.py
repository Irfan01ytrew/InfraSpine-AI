"""Tests for Step Z slice 6 — voice transcript language tagging.

Pure unit tests (``--noconftest``): no DB, no network, no Whisper model.
The tagger is the same shared detector typed chat uses, so a Roman Urdu
voice note and a typed Roman Urdu question classify the same way. The
tag is metadata only — it never changes authorization or what the asker
may read — and the staged audio is still deleted before returning.
"""

import asyncio

import pytest

from app.rag.voice_questions import (
    TranscribedVoiceQuestion,
    tag_transcript_language,
    transcribe_voice_upload,
)


def test_tag_roman_urdu_voice_note():
    assert tag_transcript_language("aaj ka match kab hai") == "roman_urdu"
    assert tag_transcript_language("pichle mahine ka kharcha dikhao") == "roman_urdu"


def test_tag_english_voice_note():
    assert tag_transcript_language("What is the refund window?") == "en"
    assert tag_transcript_language("Show me last month's invoices") == "en"


def test_tag_urdu_script_voice_note():
    assert tag_transcript_language("آج کا میچ کب ہے") == "urdu"


def test_tag_mixed_and_unknown():
    assert tag_transcript_language("یہ file kya hai") == "mixed"
    assert tag_transcript_language("") == "unknown"
    assert tag_transcript_language("12345") == "unknown"


def test_tag_matches_typed_chat_detector():
    """Same input, same answer as the detector typed chat uses."""
    from app.language import detect_language

    for text in (
        "aaj ka match kab hai",
        "What is the refund window?",
        "آج کا میچ کب ہے",
        "یہ file kya hai",
        "",
    ):
        assert tag_transcript_language(text) == detect_language(text)


class _FakeAudio:
    """Minimal AudioUpload: one bounded read, then EOF."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.size = len(data)
        self._data = data
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        chunk, self._data = self._data, b""
        return chunk

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


def test_upload_returns_tagged_transcript():
    audio = _FakeAudio("question.wav", b"synthetic audio only")

    result = _run(
        transcribe_voice_upload(
            audio,
            max_bytes=8 * 1024 * 1024,
            transcriber=lambda _path: "aaj ka match kab hai",
        )
    )

    assert isinstance(result, TranscribedVoiceQuestion)
    assert result.text == "aaj ka match kab hai"
    assert result.language == "roman_urdu"
    assert result.suffix == ".wav"
    assert result.byte_count == len(b"synthetic audio only")
    assert audio.closed


def test_upload_tags_english_transcript():
    audio = _FakeAudio("question.mp3", b"synthetic audio only")

    result = _run(
        transcribe_voice_upload(
            audio,
            max_bytes=8 * 1024 * 1024,
            transcriber=lambda _path: "What is the refund window?",
        )
    )

    assert result.language == "en"
    assert result.suffix == ".mp3"


def test_upload_rejects_empty_transcript():
    audio = _FakeAudio("question.wav", b"synthetic audio only")
    with pytest.raises(Exception, match="No speech could be recognized"):
        _run(
            transcribe_voice_upload(
                audio,
                max_bytes=8 * 1024 * 1024,
                transcriber=lambda _path: "   ",
            )
        )


def test_upload_rejects_unsupported_suffix():
    audio = _FakeAudio("question.txt", b"not audio")
    with pytest.raises(Exception, match="supported audio file"):
        _run(transcribe_voice_upload(audio, max_bytes=8 * 1024 * 1024))
    assert audio.closed
