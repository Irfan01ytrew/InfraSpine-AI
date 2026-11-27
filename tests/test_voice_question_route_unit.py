"""Database-free request-handler acceptance for the transient voice-answer route."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi import Response
from starlette.requests import Request

from app.core.errors import FileRejectedError, FileTooLargeError
from app.schemas.rag import RagChatResponse

def _run_inline(coroutine):
    """Drive the route while its database and provider collaborators are immediate fakes."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("A fake route collaborator unexpectedly waited on external I/O.")


class _Audio:
    def __init__(self, filename: str, *, size: int | None = 10):
        self.filename = filename
        self.size = size
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Db:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@dataclass
class _Settings:
    rag_trace_enabled: bool = True
    rag_query_analysis: bool = True

    def model_copy(self, *, update: dict[str, object]):
        return replace(self, **update)


class _Runtime:
    def __init__(self):
        self.choice = None
        self.client = object()

    async def get_client(self, _db, choice):
        self.choice = choice
        return self.client


def _request() -> Request:
    app = SimpleNamespace(state=SimpleNamespace(llm_runtime=_Runtime()))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/rag/voice-question",
        "raw_path": b"/api/rag/voice-question",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
        "app": app,
    })


def _answer() -> RagChatResponse:
    return RagChatResponse(
        answer="Synthetic answer.",
        insufficient_evidence=False,
        citations=[],
        retrieved_chunks=0,
        strategy="general",
        model="synthetic-model",
        provider="synthetic-provider",
    )


def test_voice_route_uses_transient_scoped_rag_and_non_sensitive_audit(tmp_path, monkeypatch):
    from app.api.routes import rag as rag_route

    request = _request()
    response = Response()
    db = _Db()
    settings = _Settings()
    audio = _Audio("question.wav")
    user = SimpleNamespace(id=uuid.uuid4(), email="synthetic@example.invalid")
    focus_id = uuid.uuid4()
    transcript = "What is the refund window?"
    staged_audio = Path(tmp_path) / "question.wav"
    staged_audio.write_bytes(b"synthetic audio only")
    seen: dict[str, object] = {}
    audit_calls: list[dict[str, object]] = []

    async def fake_transcribe(upload, *, max_bytes):
        assert upload is audio and max_bytes == 8 * 1024 * 1024
        assert staged_audio.is_file()
        await upload.close()
        staged_audio.unlink()
        staged_audio.parent.rmdir()
        return transcript, ".wav", len(b"synthetic audio only")

    async def fake_rag_respond(_db, _user, _rag, voice_settings, client_factory, question, **kwargs):
        assert not staged_audio.exists() and not staged_audio.parent.exists()
        seen.update(settings=voice_settings, client_factory=client_factory, question=question, **kwargs)
        return _answer()

    monkeypatch.setattr(rag_route, "voice_question_file_limit", lambda: 8 * 1024 * 1024)
    monkeypatch.setattr(rag_route, "transcribe_voice_upload", fake_transcribe)
    monkeypatch.setattr(rag_route, "rag_respond", fake_rag_respond)
    monkeypatch.setattr(rag_route, "audit_context", lambda *_args: "synthetic-audit-context")
    monkeypatch.setattr(
        rag_route.audit,
        "record",
        lambda _db, _context, action, *, details: audit_calls.append({"action": action, **details}),
    )

    result = _run_inline(
        rag_route.spoken_knowledge_question(
            request,
            db,
            user,
            settings,
            object(),
            response,
            audio,
            focus_document_id=focus_id,
            provider="synthetic-provider",
            model="synthetic-model",
        )
    )

    assert result.transcript == transcript and result.answer.answer == "Synthetic answer."
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert audio.closed and db.commits == 1 and db.rollbacks == 0
    assert seen["question"] == transcript
    assert seen["history"] == [] and seen["memory"] is None
    assert seen["document_ids"] is None and seen["focus_document_ids"] == [focus_id]
    assert seen["settings"].rag_trace_enabled is False
    assert seen["settings"].rag_query_analysis is False
    assert settings.rag_trace_enabled is True and settings.rag_query_analysis is True
    assert _run_inline(seen["client_factory"]()) is request.app.state.llm_runtime.client
    assert request.app.state.llm_runtime.choice == ("synthetic-provider", "synthetic-model")
    assert audit_calls == [{
        "action": "rag.voice_question_answered",
        "audio_suffix": ".wav",
        "audio_bytes": len(b"synthetic audio only"),
        "transcript_chars": len(transcript),
    }]
    assert transcript not in repr(audit_calls)


@pytest.mark.parametrize(
    ("filename", "size", "error_type"),
    [
        ("recording.pdf", 10, FileRejectedError),
        ("question.wav", 9 * 1024 * 1024, FileTooLargeError),
    ],
)
def test_voice_route_rejects_invalid_upload_before_transcription(filename, size, error_type, monkeypatch):
    from app.api.routes import rag as rag_route

    audio = _Audio(filename, size=size)
    monkeypatch.setattr(rag_route, "voice_question_file_limit", lambda: 8 * 1024 * 1024)
    monkeypatch.setattr(
        rag_route,
        "transcribe_voice_upload",
        lambda *_args, **_kwargs: pytest.fail("Invalid audio must be rejected before transcription."),
    )

    with pytest.raises(error_type):
        _run_inline(
            rag_route.spoken_knowledge_question(
                _request(),
                _Db(),
                SimpleNamespace(id=uuid.uuid4(), email="synthetic@example.invalid"),
                _Settings(),
                object(),
                Response(),
                audio,
            )
        )

    assert audio.closed
