from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.rag.parsers import media_parser
from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section
from app.rag.parsers.media_parser import AudioParser
from app.rag.parsers.registry import format_for_filename
from app.rag.voice_questions import (
    MAX_VOICE_QUESTION_BYTES,
    MAX_VOICE_QUESTION_CHARS,
    MAX_VOICE_QUESTION_SECONDS,
    supported_audio_suffix,
    transcribe_spoken_question,
    voice_question_file_limit,
)


def test_spoken_question_parser_is_capped_and_transcript_is_bounded(monkeypatch):
    calls = []

    def parse_question(_self, _path, limits, *, max_duration_seconds):
        calls.append((limits, max_duration_seconds))
        return [Section("recognized speech " * 300, Locator("time", 1, "00:00–00:30"))]

    monkeypatch.setattr(AudioParser, "parse_spoken_question", parse_question)
    transcript = transcribe_spoken_question(Path("temporary.wav"))

    assert calls[0][1] == MAX_VOICE_QUESTION_SECONDS == 120
    assert calls[0][0].max_chars == MAX_VOICE_QUESTION_CHARS == 4000
    assert len(transcript) <= MAX_VOICE_QUESTION_CHARS


def test_spoken_question_rejects_declared_duration_before_loading_whisper(monkeypatch):
    class FakeContainer:
        duration = 121 * 1_000_000
        streams = SimpleNamespace(audio=[object()], video=[])

        def close(self):
            self.closed = True

    container = FakeContainer()
    fake_av = SimpleNamespace(time_base=1_000_000, open=lambda *_args, **_kwargs: container)
    settings = SimpleNamespace(
        rag_media_max_file_bytes=8 * 1024 * 1024,
        rag_media_max_duration_seconds=14_400,
    )
    monkeypatch.setitem(sys.modules, "av", fake_av)
    monkeypatch.setattr(media_parser, "_configuration", lambda **_kwargs: (settings, None))

    class FakePath:
        def stat(self):
            return SimpleNamespace(st_size=1)

        def __str__(self):
            return "synthetic.wav"

    limits = ParseLimits(max_units=1, max_chars=4000, max_unpacked_bytes=0, section_chars=1000)
    with pytest.raises(ParseError, match="duration") as exc_info:
        AudioParser().parse_spoken_question(
            FakePath(), limits, max_duration_seconds=MAX_VOICE_QUESTION_SECONDS
        )
    assert exc_info.value.code == "media_too_long"
    assert container.closed


def test_voice_uploads_accept_only_supported_audio_suffixes():
    assert supported_audio_suffix("question.WAV") == ".wav"
    assert supported_audio_suffix("question.mp3") == ".mp3"
    assert supported_audio_suffix("recording.webm") == ".webm"
    assert format_for_filename("recording.webm") == "video"
    assert supported_audio_suffix("video.mp4") is None
    assert supported_audio_suffix("question.wav.exe") is None
    assert supported_audio_suffix(None) is None


def test_voice_upload_limit_is_minimum_of_route_and_admin_media_limits(monkeypatch):
    class Settings:
        rag_media_max_file_bytes = 2 * 1024 * 1024

    monkeypatch.setattr("app.rag.voice_questions.get_settings", lambda: Settings())
    assert voice_question_file_limit() == 2 * 1024 * 1024
    Settings.rag_media_max_file_bytes = 16 * 1024 * 1024
    assert voice_question_file_limit() == MAX_VOICE_QUESTION_BYTES


