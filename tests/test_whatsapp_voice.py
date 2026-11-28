"""Step F slice 4: WhatsApp voice notes (local Whisper first, API fallback).

Plan ``docs/step-f-whatsapp-plan.md`` §6, on a throwaway ``*_test`` database
only, with the Meta transport and the transcriber stubbed — no network:

* voice module: MIME mapping, Baileys base64 decode, bounded Cloud media
  download (Meta hosts only, declared size checked first), local-first
  transcription with the API fallback only when local is unavailable, staged
  audio removed before the helper returns, oversized audio rejected;
* provider: the Whisper-compatible ``/audio/transcriptions`` call on an
  OpenAI-compatible client (DeepSeek and Gemini excluded), errors mapped to
  LLMUnavailableError; the runtime resolves the fallback provider only;
* Cloud worker: an audio webhook stores the media id, the worker transcribes
  it and the transcript (marked as machine text) becomes the task; a failed
  transcription replies "[NEEDS INPUT]" without crashing and without an
  agent run; an oversized note is rejected the same way;
* Baileys worker: a bridge voice event is admitted, transcribed, and run as
  a task; an oversized note replies "[NEEDS INPUT]" without an agent run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.agent_studio.service import AgentStudioService
from app.agent_studio.unified import StudioHost, configure_host
from app.core.config import Settings, get_settings
from app.core.errors import (
    FileRejectedError,
    FileTooLargeError,
    InvalidInputError,
    LLMUnavailableError,
)
from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppInbound
from app.db.models import User
from app.integrations import whatsapp as cloud_integration
from app.integrations import whatsapp_voice
from app.integrations.transport import Response as TransportResponse, TransportError
from app.providers.openai_compatible import OpenAICompatibleClient
from app.rag.parsers.base import ParseError
from app.services import agent_studio_baileys
from app.services.agent_studio_baileys import BridgeSession, seal
from app.services.llm import ClientParams
from app.services.llm_runtime import LLMRuntime
from app.main import app
from tests.conftest import ADMIN_EMAIL, CSRF, run_db
from tests.test_agent_studio_unified import _stub_execute
from tests.test_whatsapp_unified_intake import _post, _seed_connection

SENDER = "30000000"
MEDIA_ID = "123456789"
MEDIA_URL = "https://scontent.xx.fbcdn.net/v/voice.ogg?token=synthetic"
TRANSCRIPT = "laptop ki battery kitni hai"


# ---------------------------------------------------------------------------
# Voice module: pure unit tests (no database, no network)
# ---------------------------------------------------------------------------

def test_suffix_for_mime_maps_whatsapp_voice_notes():
    assert whatsapp_voice.suffix_for_mime("audio/ogg") == ".ogg"
    assert whatsapp_voice.suffix_for_mime("audio/ogg; codecs=opus") == ".ogg"
    assert whatsapp_voice.suffix_for_mime("audio/mpeg") == ".mp3"
    assert whatsapp_voice.suffix_for_mime("audio/mp4") == ".m4a"
    assert whatsapp_voice.suffix_for_mime("audio/x-wav") == ".wav"
    assert whatsapp_voice.suffix_for_mime("audio/zip") is None
    assert whatsapp_voice.suffix_for_mime(None) is None
    assert whatsapp_voice.mark_machine_text("  hello  ") == "[Voice note transcript] hello"


def test_decode_baileys_audio_bounds_and_rejects_bad_input():
    encoded = base64.b64encode(b"fake ogg bytes").decode()
    data, suffix = whatsapp_voice.decode_baileys_audio(encoded, "audio/ogg; codecs=opus")
    assert (data, suffix) == (b"fake ogg bytes", ".ogg")
    with pytest.raises(FileRejectedError):
        whatsapp_voice.decode_baileys_audio("not-base64!!", "audio/ogg")
    with pytest.raises(FileRejectedError):
        whatsapp_voice.decode_baileys_audio(encoded, "audio/zip")
    with pytest.raises(FileRejectedError):
        whatsapp_voice.decode_baileys_audio("", "audio/ogg")
    oversized = base64.b64encode(b"x" * (whatsapp_voice.MAX_WHATSAPP_VOICE_BYTES + 1)).decode()
    with pytest.raises(FileTooLargeError):
        whatsapp_voice.decode_baileys_audio(oversized, "audio/ogg")


def _run(coro):
    return asyncio.run(coro)


def _temporary_directory(created: list[Path]):
    def create(**kwargs):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], **kwargs)
        created.append(Path(temporary.name))
        return temporary

    return create


async def _inline_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_transcribe_voice_note_uses_local_first_and_removes_audio():
    staged: list[Path] = []
    temporary: list[Path] = []
    api_calls: list[Path] = []

    def local_transcriber(path: Path) -> str:
        assert path.is_file()
        staged.append(path)
        return "  hello from the voice note  "

    async def api_transcriber(path: Path) -> str:
        api_calls.append(path)
        return "must not be used"

    transcript, source = _run(whatsapp_voice.transcribe_voice_note(
        b"fake ogg bytes", ".ogg",
        transcriber=local_transcriber, api_transcriber=api_transcriber,
        run_in_thread=_inline_thread, temporary_directory=_temporary_directory(temporary),
    ))
    assert (transcript, source) == ("hello from the voice note", "local")
    assert api_calls == []
    assert staged and not staged[0].exists() and not staged[0].parent.exists()
    assert temporary and not temporary[0].exists()


def test_transcribe_voice_note_falls_back_to_api_when_local_unavailable():
    def local_transcriber(_path: Path) -> str:
        raise ParseError("media_model_unavailable", "No local speech model is configured.")

    async def api_transcriber(_path: Path) -> str:
        return "fallback transcript"

    transcript, source = _run(whatsapp_voice.transcribe_voice_note(
        b"fake ogg bytes", ".ogg",
        transcriber=local_transcriber, api_transcriber=api_transcriber,
        run_in_thread=_inline_thread, temporary_directory=_temporary_directory([]),
    ))
    assert (transcript, source) == ("fallback transcript", "api")


def test_transcribe_voice_note_fails_closed_when_no_fallback():
    def local_transcriber(_path: Path) -> str:
        raise ParseError("media_runtime_unavailable", "No local transcription runtime.")

    with pytest.raises(InvalidInputError, match="unavailable"):
        _run(whatsapp_voice.transcribe_voice_note(
            b"fake ogg bytes", ".ogg",
            transcriber=local_transcriber, run_in_thread=_inline_thread,
            temporary_directory=_temporary_directory([]),
        ))


def test_transcribe_voice_note_rejects_unintelligible_audio_without_api():
    api_calls: list[Path] = []

    def local_transcriber(_path: Path) -> str:
        raise ParseError("no_text", "No speech could be recognized in this recording.")

    async def api_transcriber(path: Path) -> str:
        api_calls.append(path)
        return "must not be tried for unintelligible audio"

    with pytest.raises(FileRejectedError, match="No speech"):
        _run(whatsapp_voice.transcribe_voice_note(
            b"fake ogg bytes", ".ogg",
            transcriber=local_transcriber, api_transcriber=api_transcriber,
            run_in_thread=_inline_thread, temporary_directory=_temporary_directory([]),
        ))
    assert api_calls == []


def test_transcribe_voice_note_rejects_oversized_bytes():
    with pytest.raises(FileTooLargeError):
        _run(whatsapp_voice.transcribe_voice_note(
            b"x" * (whatsapp_voice.MAX_WHATSAPP_VOICE_BYTES + 1), ".ogg",
            transcriber=lambda _path: pytest.fail("Oversized audio must not be transcribed."),
            run_in_thread=_inline_thread, temporary_directory=_temporary_directory([]),
        ))


def _meta_response(url=None, mime="audio/ogg", file_size=21):
    return TransportResponse(status=200, body={
        "url": url or MEDIA_URL, "mime_type": mime, "file_size": file_size,
    })


def test_download_cloud_audio_uses_meta_url_and_bounds_bytes():
    fetched: list[str] = []

    async def meta_request(_base, path, **_kwargs):
        assert path == "/" + MEDIA_ID
        return _meta_response()

    async def fetch_bytes(url: str) -> bytes:
        fetched.append(url)
        return b"fake ogg bytes"

    data, suffix = _run(whatsapp_voice.download_cloud_audio(
        MEDIA_ID, api_version="v21.0", access_token="synthetic-token",
        meta_request=meta_request, fetch_bytes=fetch_bytes,
    ))
    assert (data, suffix) == (b"fake ogg bytes", ".ogg")
    assert fetched == [MEDIA_URL]


def test_download_cloud_audio_rejects_non_meta_url_and_oversize_before_fetch():
    fetched: list[str] = []

    async def evil_meta_request(_base, _path, **_kwargs):
        return _meta_response(url="https://evil.example.com/voice.ogg")

    async def fetch_bytes(url: str) -> bytes:
        fetched.append(url)
        return b"fake ogg bytes"

    with pytest.raises(TransportError):
        _run(whatsapp_voice.download_cloud_audio(
            MEDIA_ID, api_version="v21.0", access_token="synthetic-token",
            meta_request=evil_meta_request, fetch_bytes=fetch_bytes,
        ))
    assert fetched == []

    async def big_meta_request(_base, _path, **_kwargs):
        return _meta_response(file_size=whatsapp_voice.MAX_WHATSAPP_VOICE_BYTES + 1)

    with pytest.raises(FileTooLargeError):
        _run(whatsapp_voice.download_cloud_audio(
            MEDIA_ID, api_version="v21.0", access_token="synthetic-token",
            meta_request=big_meta_request, fetch_bytes=fetch_bytes,
        ))
    assert fetched == []


# ---------------------------------------------------------------------------
# Provider: Whisper-compatible transcription on an OpenAI-compatible client
# ---------------------------------------------------------------------------

def _client(provider="custom-test", base_url="https://api.example.com/v1"):
    return OpenAICompatibleClient(
        provider, base_url,
        params=ClientParams(api_key="sk-test", model="chat-model", settings={},
                            timeout_seconds=5, max_retries=0),
    )


def test_supports_audio_transcription_excludes_deepseek_and_gemini():
    assert _client("deepseek", "https://api.deepseek.com/v1").supports_audio_transcription is False
    assert _client("custom-gemini", "https://generativelanguage.googleapis.com/v1beta/openai/").supports_audio_transcription is False
    assert _client().supports_audio_transcription is True


def test_transcribe_audio_posts_whisper_and_returns_text(monkeypatch):
    client = _client()
    calls = {}

    async def fake_create(*, file, model, **_kwargs):
        calls["file"] = file
        calls["model"] = model
        return SimpleNamespace(text="  hello from the provider  ")

    monkeypatch.setattr(client._client.audio.transcriptions, "create", fake_create)
    text = _run(client.transcribe_audio(b"fake ogg bytes", filename="voice.ogg", mime_type="audio/ogg"))
    assert text == "hello from the provider"
    assert calls["model"] == "whisper-1"
    assert calls["file"][0] == "voice.ogg"
    assert calls["file"][1] == b"fake ogg bytes"


def test_transcribe_audio_maps_provider_errors_to_unavailable(monkeypatch):
    client = _client()

    async def fake_create(*_args, **_kwargs):
        raise openai.APIStatusError(
            "not found",
            response=httpx.Response(404, request=httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")),
            body=None,
        )

    monkeypatch.setattr(client._client.audio.transcriptions, "create", fake_create)
    with pytest.raises(LLMUnavailableError):
        _run(client.transcribe_audio(b"fake ogg bytes", filename="voice.ogg", mime_type="audio/ogg"))


def test_runtime_audio_client_requires_a_configured_fallback():
    runtime = LLMRuntime(Settings(llm_fallback_provider=""), cipher=None)
    with pytest.raises(LLMUnavailableError):
        _run(runtime.get_audio_transcription_client(None))


# ---------------------------------------------------------------------------
# Cloud worker: drives the real process() with a stubbed Meta + transcriber
# ---------------------------------------------------------------------------

_META = {"calls": [], "file_size": 21}


async def _voice_meta_request(base_url, path, **kwargs):
    """Mocked Meta Graph API: media metadata, or a message-send answer."""
    _META["calls"].append((base_url, path, kwargs.get("method", "GET"), kwargs.get("payload")))
    if path == "/" + MEDIA_ID:
        return _meta_response(file_size=_META["file_size"])
    return TransportResponse(status=200, body={"messages": [{"id": "wamid.synthetic-reply"}]})


async def _noop_worker(*_args, **_kwargs):
    return None


@pytest.fixture
def voice_client(monkeypatch):
    """A TestClient with Meta stubbed, the transcriber stubbed, and the agent stubbed."""
    monkeypatch.setattr(cloud_integration, "run_whatsapp_agent_worker", _noop_worker)
    monkeypatch.setattr(cloud_integration, "provider_request", _voice_meta_request)
    monkeypatch.setattr(whatsapp_voice, "provider_request", _voice_meta_request)
    monkeypatch.setattr(whatsapp_voice, "_download_bytes", lambda url: _fake_bytes(url))
    monkeypatch.setattr(whatsapp_voice, "transcribe_spoken_question", lambda _path: TRANSCRIPT)
    _stub_execute(monkeypatch)
    _META["calls"].clear()
    _META["file_size"] = 21
    with TestClient(app, headers=CSRF) as test_client:
        configure_host(StudioHost(app.state.sessionmaker, get_settings(), app.state.cipher,
                                  app.state.llm_runtime, app.state.rag))
        yield test_client
        configure_host(None)


async def _fake_bytes(_url: str) -> bytes:
    return b"fake ogg bytes"


def _audio_payload(*, message_id="wamid.voice1", sender=SENDER):
    data = json.dumps({"entry": [{"id": "100000", "changes": [{"value": {
        "metadata": {"phone_number_id": "200000"},
        "messages": [{"id": message_id, "from": sender, "type": "audio",
                      "audio": {"id": MEDIA_ID, "mime_type": "audio/ogg"},
                      "timestamp": "1795800000"}],
    }}]}]}).encode()
    return data


def _record_start_run(monkeypatch):
    """Record the task text each agent run receives, then run the real start_run."""
    recorded: list[str] = []
    original = AgentStudioService.start_run

    async def recording(self, agent_id, thread_id, prompt, **kwargs):
        recorded.append(prompt)
        return await original(self, agent_id, thread_id, prompt, **kwargs)

    monkeypatch.setattr(AgentStudioService, "start_run", recording)
    return recorded


def _drive_intake(client):
    """Claim the queued inbound and run the Cloud worker intake once."""
    settings = Settings(workflow_engine_worker_enabled=True)

    async def drive():
        identity = await cloud_integration.claim(app.state.sessionmaker)
        assert identity is not None, "no queued WhatsApp inbound to claim"
        await cloud_integration.process(app.state.sessionmaker, identity, settings,
                                        app.state.cipher, app.state.llm_runtime, app.state.rag)

    client.portal.call(drive)


def _inbound_row(connection):
    async def row(db):
        return (await db.execute(select(
            AgentStudioWhatsAppInbound.id, AgentStudioWhatsAppInbound.status,
            AgentStudioWhatsAppInbound.message_type, AgentStudioWhatsAppInbound.text_ciphertext,
            AgentStudioWhatsAppInbound.text_nonce, AgentStudioWhatsAppInbound.text_key_id,
            AgentStudioWhatsAppInbound.reply_ciphertext, AgentStudioWhatsAppInbound.reply_nonce,
            AgentStudioWhatsAppInbound.reply_key_id,
        ).where(AgentStudioWhatsAppInbound.connection_id == connection))).one()

    return run_db(row)


def _reply_text(row):
    from app.services.agent_studio_whatsapp import _open

    return _open(app.state.cipher, "reply", row[0], row[6], row[7], row[8])


def test_audio_webhook_stores_media_id_for_the_worker(voice_client):
    connection, _agent = _seed_connection(voice_client)
    assert _post(voice_client, connection, _audio_payload()).status_code == 200
    row = _inbound_row(connection)
    assert row[2] == "audio"
    from app.services.agent_studio_whatsapp import _open

    assert _open(app.state.cipher, "inbound", row[0], row[3], row[4], row[5]) == MEDIA_ID


def test_cloud_voice_note_transcript_becomes_the_task(voice_client, monkeypatch):
    connection, _agent = _seed_connection(voice_client)
    recorded = _record_start_run(monkeypatch)
    assert _post(voice_client, connection, _audio_payload()).status_code == 200
    _drive_intake(voice_client)

    assert recorded == [whatsapp_voice.VOICE_TRANSCRIPT_PREFIX + TRANSCRIPT]
    row = _inbound_row(connection)
    assert row[1] == "completed"
    assert row[2] == "voice"
    # Step F slice 5: the run completes inline (no execute_once row), so the
    # reply is the bare answer — no "[SUCCESS]" without verification.
    assert _reply_text(row) == "Verified synthetic result."
    sends = [call for call in _META["calls"] if call[2] == "POST"]
    assert len(sends) == 2  # progress + final, both through the Cloud path


def test_cloud_voice_transcription_failure_replies_needs_input(voice_client, monkeypatch):
    connection, _agent = _seed_connection(voice_client)
    recorded = _record_start_run(monkeypatch)

    def failing_transcriber(_path):
        raise ParseError("media_transcription_failed", "Local speech transcription failed for this file.")

    monkeypatch.setattr(whatsapp_voice, "transcribe_spoken_question", failing_transcriber)
    assert _post(voice_client, connection, _audio_payload()).status_code == 200
    _drive_intake(voice_client)

    assert recorded == []  # no agent run for an unusable voice note
    row = _inbound_row(connection)
    assert row[1] == "completed"
    assert _reply_text(row).startswith("[NEEDS INPUT] I could not understand that voice note.")


def test_cloud_oversized_voice_note_is_rejected(voice_client, monkeypatch):
    connection, _agent = _seed_connection(voice_client)
    recorded = _record_start_run(monkeypatch)
    _META["file_size"] = whatsapp_voice.MAX_WHATSAPP_VOICE_BYTES + 1
    assert _post(voice_client, connection, _audio_payload()).status_code == 200
    _drive_intake(voice_client)

    assert recorded == []
    row = _inbound_row(connection)
    assert row[1] == "completed"
    assert _reply_text(row).startswith("[NEEDS INPUT] I could not understand that voice note.")


# ---------------------------------------------------------------------------
# Baileys worker: drives the real BridgeSession.execute with a stubbed bridge
# ---------------------------------------------------------------------------

def _seed_baileys_message(monkeypatch, client, agent, payload):
    """A Baileys connection + queued message for the admin; returns (connection, message, bridge)."""
    message_id = uuid.uuid4()
    baileys_connection = uuid.uuid4()

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        await db.execute(insert(BaileysConnection).values(
            id=baileys_connection, owner_id=owner, agent_id=agent))
        await db.execute(insert(BaileysMessage).values(
            id=message_id, connection_id=baileys_connection, external_id=message_id.hex,
            status="queued",
            payload_sealed=seal(app.state.cipher, f"message:{message_id}", json.dumps(payload)),
        ))

    run_db(seed)
    bridge = BridgeSession(app, get_settings(), baileys_connection)
    bridge.online.set()
    sent = []

    async def fake_send_reply_once(_db, _user, _message_id, phase, delivery_id, jid, reply, *, timeout):
        sent.append((phase, delivery_id, jid, reply))
        return True

    monkeypatch.setattr(bridge, "send_reply_once", fake_send_reply_once)
    return baileys_connection, message_id, bridge, sent


@pytest.fixture
def baileys_client(monkeypatch):
    """A TestClient with the agent execution stubbed (no bridge process)."""
    monkeypatch.setattr(agent_studio_baileys, "bridge_available", lambda: False)
    monkeypatch.setattr(whatsapp_voice, "transcribe_spoken_question", lambda _path: TRANSCRIPT)
    _stub_execute(monkeypatch)
    with TestClient(app, headers=CSRF) as test_client:
        configure_host(StudioHost(app.state.sessionmaker, get_settings(), app.state.cipher,
                                  app.state.llm_runtime, app.state.rag))
        yield test_client
        configure_host(None)


def _baileys_message_state(message_id):
    async def state(db):
        row = (await db.execute(select(
            BaileysMessage.status, BaileysMessage.reply_sealed,
        ).where(BaileysMessage.id == message_id))).one()
        return row

    return run_db(state)


def test_baileys_voice_note_transcript_becomes_the_task(baileys_client, monkeypatch):
    _cloud, agent = _seed_connection(baileys_client)
    recorded = _record_start_run(monkeypatch)
    encoded = base64.b64encode(b"fake ogg bytes").decode()
    _conn, message_id, bridge, sent = _seed_baileys_message(monkeypatch, baileys_client, agent, {
        "text": "", "jid": "10000000@s.whatsapp.net",
        "audio": encoded, "mime": "audio/ogg; codecs=opus", "audio_too_large": False,
    })

    async def run():
        await bridge.execute(message_id)

    baileys_client.portal.call(run)

    assert recorded == [whatsapp_voice.VOICE_TRANSCRIPT_PREFIX + TRANSCRIPT]
    status, reply_sealed = _baileys_message_state(message_id)
    assert status == "completed"
    reply = agent_studio_baileys.unseal(app.state.cipher, f"reply:{message_id}", reply_sealed)
    assert reply.startswith("InfraSpine AI ·")
    assert "*Result*" in reply
    assert [phase for phase, *_rest in sent] == ["ack", "final"]


def test_baileys_oversized_voice_note_replies_needs_input(baileys_client, monkeypatch):
    _cloud, agent = _seed_connection(baileys_client)
    recorded = _record_start_run(monkeypatch)
    _conn, message_id, bridge, sent = _seed_baileys_message(monkeypatch, baileys_client, agent, {
        "text": "", "jid": "10000000@s.whatsapp.net",
        "audio": None, "mime": "", "audio_too_large": True,
    })

    async def run():
        await bridge.execute(message_id)

    baileys_client.portal.call(run)

    assert recorded == []
    status, reply_sealed = _baileys_message_state(message_id)
    assert status == "completed"
    reply = agent_studio_baileys.unseal(app.state.cipher, f"reply:{message_id}", reply_sealed)
    assert "[NEEDS INPUT] That voice note is too large" in reply
    assert [phase for phase, *_rest in sent] == ["final"]


def test_baileys_voice_event_is_admitted_to_the_queue(baileys_client, monkeypatch):
    _cloud, agent = _seed_connection(baileys_client)
    encoded = base64.b64encode(b"fake ogg bytes").decode()

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        connection = uuid.uuid4()
        await db.execute(insert(BaileysConnection).values(
            id=connection, owner_id=owner, agent_id=agent))
        return connection

    connection = run_db(seed)
    bridge = BridgeSession(app, get_settings(), connection)

    async def emit():
        await bridge.event({
            "type": "message", "id": "VOICE123", "jid": "10000000@s.whatsapp.net",
            "text": "", "audio": encoded, "mime": "audio/ogg",
        })

    baileys_client.portal.call(emit)

    async def queued(db):
        row = (await db.execute(select(BaileysMessage.payload_sealed).where(
            BaileysMessage.connection_id == connection))).one_or_none()
        return row[0] if row else None

    sealed = run_db(queued)
    assert sealed is not None
    # The queued payload carries the audio (it is sealed, so unseal to check).
    message_id = run_db(lambda db: db.scalar(select(BaileysMessage.id).where(
        BaileysMessage.connection_id == connection)))
    payload = json.loads(agent_studio_baileys.unseal(app.state.cipher, f"message:{message_id}", sealed))
    assert payload["audio"] == encoded
    assert payload["mime"] == "audio/ogg"
    assert payload["audio_too_large"] is False
