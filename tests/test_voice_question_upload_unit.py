"""Database-free tests for bounded, temporary voice upload handling."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.core.errors import FileRejectedError, FileTooLargeError, InvalidInputError
from app.rag.parsers.base import ParseError
from app.rag.voice_questions import transcribe_voice_upload

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Audio:
    def __init__(self, filename: str, chunks: list[bytes], *, size: int | None = None):
        self.filename = filename
        self.size = size
        self.chunks = iter(chunks)
        self.closed = False

    async def read(self, _size: int = -1) -> bytes:
        return next(self.chunks, b"")

    async def close(self) -> None:
        self.closed = True


def _run_inline(coroutine):
    """Drive the helper while its upload and transcription collaborators are immediate fakes."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("A fake voice upload collaborator unexpectedly waited on external I/O.")


async def _inline_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def _temporary_directory(created: list[Path]):
    def create(**kwargs):
        temporary = tempfile.TemporaryDirectory(dir=_PROJECT_ROOT, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    return create


def test_upload_is_bounded_transcribed_and_removed_before_helper_returns():
    audio_bytes = b"synthetic audio only"
    audio = _Audio("question.wav", [audio_bytes], size=len(audio_bytes))
    staged_paths: list[Path] = []
    temporary_paths: list[Path] = []

    def transcriber(path: Path) -> str:
        assert path.is_file()
        assert path.read_bytes() == audio_bytes
        staged_paths.append(path)
        return "  What is the refund window?  "

    transcript, suffix, byte_count = _run_inline(
        transcribe_voice_upload(
            audio,
            max_bytes=1024,
            transcriber=transcriber,
            temporary_directory=_temporary_directory(temporary_paths),
            run_in_thread=_inline_thread,
        )
    )

    assert (transcript, suffix, byte_count) == ("What is the refund window?", ".wav", len(audio_bytes))
    assert audio.closed
    assert staged_paths and not staged_paths[0].exists() and not staged_paths[0].parent.exists()
    assert temporary_paths and not temporary_paths[0].exists()


def test_upload_enforces_size_while_streaming_and_closes_upload():
    audio = _Audio("question.wav", [b"123456", b"78901"], size=None)
    temporary_paths: list[Path] = []

    with pytest.raises(FileTooLargeError):
        _run_inline(
            transcribe_voice_upload(
                audio,
                max_bytes=10,
                transcriber=lambda _path: pytest.fail("Oversized audio must not be transcribed."),
                temporary_directory=_temporary_directory(temporary_paths),
                run_in_thread=_inline_thread,
            )
        )

    assert audio.closed
    assert temporary_paths and not temporary_paths[0].exists()


def test_upload_rejects_unsupported_suffix_and_declared_oversize():
    unsupported = _Audio("recording.pdf", [b"synthetic audio"])
    temporary_paths: list[Path] = []
    with pytest.raises(FileRejectedError):
        _run_inline(
            transcribe_voice_upload(
                unsupported,
                max_bytes=100,
                temporary_directory=_temporary_directory(temporary_paths),
                run_in_thread=_inline_thread,
            )
        )
    assert unsupported.closed

    declared_oversize = _Audio("question.wav", [b"synthetic audio"], size=101)
    with pytest.raises(FileTooLargeError):
        _run_inline(
            transcribe_voice_upload(
                declared_oversize,
                max_bytes=100,
                temporary_directory=_temporary_directory(temporary_paths),
                run_in_thread=_inline_thread,
            )
        )
    assert declared_oversize.closed
    assert not temporary_paths


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("no_text", FileRejectedError),
        ("media_model_unavailable", InvalidInputError),
    ],
)
def test_parser_errors_are_sanitized_and_upload_is_closed(code, error_type):
    audio = _Audio("question.wav", [b"synthetic audio"])
    temporary_paths: list[Path] = []

    def failed_transcriber(_path: Path) -> str:
        raise ParseError(code, "Synthetic parser failure.")

    with pytest.raises(error_type, match="Synthetic parser failure"):
        _run_inline(
            transcribe_voice_upload(
                audio,
                max_bytes=1024,
                transcriber=failed_transcriber,
                temporary_directory=_temporary_directory(temporary_paths),
                run_in_thread=_inline_thread,
            )
        )

    assert audio.closed
    assert temporary_paths and not temporary_paths[0].exists()
