"""Slice 5 tests for Step O (voice-first founder mode): voice replies (TTS).

* :mod:`app.voice.reply` builds the reply text deterministically from the
  proposal's stored state — pure functions, no database.
* :mod:`app.voice.tts` synthesizes audio when a provider can, and reports
  unavailable (``None``) otherwise — the route then serves the text fallback,
  never an error.
* ``GET /api/voice/proposals/{id}/reply`` serves audio when synthesis
  succeeds, JSON text fallback otherwise, with the same visibility as the
  proposal itself.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.voice.reply import build_voice_reply
from app.voice.tts import TTSAudio, synthesize
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login
from tests.test_voice_unified import (
    _decide,
    _make_manager,
    _proposal,
    _pump_until_run,
    _submit_via_service,
)
from tests.test_workflow_engine import setup_scope


# --- reply text: pure ---------------------------------------------------------

def test_reply_executed_status_overview_names_counts():
    reply = build_voice_reply(
        intent_kind="status_overview",
        status="executed",
        execution_result={"kind": "status_overview", "overview": {
            "workflow_runs": {"running": 2, "completed": 1},
            "invoice_batches": {"verifying": 2},
            "pending_approvals": 1,
        }},
    )
    assert reply.kind == "executed"
    assert reply.text == ("Done. Status overview: 3 workflow runs, 2 invoice "
                          "batches, 1 approvals pending. Nothing was changed.")


def test_reply_executed_invoice_approve_names_invoice():
    reply = build_voice_reply(
        intent_kind="invoice_approve",
        status="executed",
        execution_result={"kind": "invoice_approve", "invoice_number": "INV-42"},
    )
    assert reply.kind == "executed"
    assert reply.text == ("Done. Your approval for invoice INV-42 is recorded. "
                          "The invoice status was not changed.")


def test_reply_executed_unknown_kind_is_generic():
    reply = build_voice_reply(intent_kind="meeting_summary", status="executed")
    assert reply.text == "Done. Your voice request was executed."


def test_reply_proposed_waits_for_approval():
    reply = build_voice_reply(intent_kind="status_overview", status="proposed")
    assert reply.kind == "proposed"
    assert "waiting for approval" in reply.text


def test_reply_approved_executes():
    reply = build_voice_reply(intent_kind="status_overview", status="approved")
    assert reply.text == "Approved. Executing your request now."


def test_reply_rejected_and_expired_do_nothing():
    assert build_voice_reply(
        intent_kind="status_overview", status="rejected").text == (
        "Your request was rejected. Nothing was done.")
    assert "expired" in build_voice_reply(
        intent_kind="status_overview", status="expired").text


def test_reply_failed_names_reason_truncated():
    reply = build_voice_reply(
        intent_kind="invoice_approve", status="failed",
        failure_reason="No invoice INV-99 in this department.")
    assert reply.text == "Could not do that: No invoice INV-99 in this department."
    long_reason = "x" * 300
    reply = build_voice_reply(
        intent_kind="invoice_approve", status="failed", failure_reason=long_reason)
    assert reply.text == "Could not do that: " + "x" * 200


def test_reply_failed_without_reason_asks_for_new_note():
    reply = build_voice_reply(intent_kind="status_overview", status="failed")
    assert "new voice note" in reply.text


def test_reply_failed_strips_error_code_prefix():
    # The engine stores "not_found: <message>"; the founder hears only the message.
    reply = build_voice_reply(
        intent_kind="invoice_approve", status="failed",
        failure_reason="not_found: No invoice INV-99 in this department.")
    assert reply.text == "Could not do that: No invoice INV-99 in this department."


def test_reply_failed_keeps_human_sentence_with_colon():
    reply = build_voice_reply(
        intent_kind="invoice_approve", status="failed",
        failure_reason="Payment failed: insufficient funds.")
    assert reply.text == "Could not do that: Payment failed: insufficient funds."


def test_reply_unknown_status_falls_back_to_waiting():
    reply = build_voice_reply(intent_kind="status_overview", status="bogus")
    assert reply.kind == "proposed"


def test_reply_text_is_ascii_and_bounded():
    for status in ("executed", "proposed", "approved", "rejected", "expired", "failed"):
        reply = build_voice_reply(
            intent_kind="invoice_approve", status=status,
            execution_result={"invoice_number": "INV-42"},
            failure_reason="y" * 500)
        assert reply.text.isascii()
        assert len(reply.text) <= 500


# --- TTS providers: pure ------------------------------------------------------

def test_tts_none_provider_is_text_fallback():
    assert synthesize("Hello", provider="none") is None


def test_tts_api_provider_unconfigured_is_text_fallback():
    assert synthesize("Hello", provider="api") is None


def test_tts_unknown_provider_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown TTS provider"):
        synthesize("Hello", provider="deepseek")


def test_tts_local_without_binary_is_text_fallback(monkeypatch):
    monkeypatch.setattr("app.voice.tts.shutil.which", lambda *args: None)
    assert synthesize("Hello", provider="local") is None


def test_tts_local_uses_espeak_and_returns_wav(monkeypatch):
    tmp_path = Path(tempfile.mkdtemp(prefix="voice-tts-test-"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "espeak-ng"
    fake.write_text(
        "#!/bin/sh\n"
        "out=\"\"; while [ $# -gt 0 ]; do case \"$1\" in -w) out=\"$2\"; shift 2;; *) shift;; esac; done\n"
        "cat > /dev/null\n"
        "printf 'RIFFfake-wav' > \"$out\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    result = synthesize("Hello founder", provider="local")
    assert result is not None
    assert isinstance(result, TTSAudio)
    assert result.provider == "local"
    assert result.content_type == "audio/wav"
    assert result.audio == b"RIFFfake-wav"
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_tts_local_nonzero_exit_is_text_fallback(monkeypatch):
    tmp_path = Path(tempfile.mkdtemp(prefix="voice-tts-test-"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "espeak-ng"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert synthesize("Hello", provider="local") is None
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_tts_local_truncates_long_text(monkeypatch):
    tmp_path = Path(tempfile.mkdtemp(prefix="voice-tts-test-"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "espeak-ng"
    fake.write_text(
        "#!/bin/sh\n"
        "out=\"\"; while [ $# -gt 0 ]; do case \"$1\" in -w) out=\"$2\"; shift 2;; *) shift;; esac; done\n"
        "cat > \"$SPOKEN_FILE\"\n"
        "printf 'RIFF' > \"$out\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    spoken = tmp_path / "spoken.txt"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SPOKEN_FILE", str(spoken))
    result = synthesize("x" * 600, provider="local")
    assert result is not None
    assert len(spoken.read_bytes()) == 500
    shutil.rmtree(tmp_path, ignore_errors=True)


# --- reply route ---------------------------------------------------------------

def test_reply_route_proposed_returns_text_fallback(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/voice/proposals/{submission.proposal.id}/reply")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "proposed"
    assert "waiting for approval" in body["text"]
    assert body["audio_available"] is False
    assert body["reason"] == "tts_unavailable"
    assert body["provider"] == "none"


def test_reply_route_executed_returns_done_text(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="completed")
    assert _proposal(proposal_id)["status"] == "executed"
    login(client, ADMIN_EMAIL)
    body = client.get(f"/api/voice/proposals/{proposal_id}/reply").json()
    assert body["status"] == "executed"
    assert body["text"].startswith("Done. Status overview:")
    assert body["audio_available"] is False


def test_reply_route_failed_names_reason(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "Approve invoice INV-99 please")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="failed")
    login(client, ADMIN_EMAIL)
    body = client.get(f"/api/voice/proposals/{proposal_id}/reply").json()
    assert body["status"] == "failed"
    assert body["text"] == "Could not do that: No invoice INV-99 in this department."


def test_reply_route_hides_other_users_proposals(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    login(client, USER_EMAIL)
    response = client.get(f"/api/voice/proposals/{submission.proposal.id}/reply")
    assert response.status_code == 404


def test_reply_route_serves_audio_when_provider_synthesizes(client, monkeypatch):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")

    def fake_synthesize(text, *, provider="none", timeout=30):
        assert provider == "none"  # default setting in tests
        return TTSAudio(audio=b"fake-wav-bytes", content_type="audio/wav", provider="local")

    monkeypatch.setattr("app.api.routes.voice.synthesize", fake_synthesize)
    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/voice/proposals/{submission.proposal.id}/reply")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"fake-wav-bytes"
    assert "waiting for approval" in response.headers["x-voice-reply-text"]
    assert response.headers["x-voice-reply-status"] == "proposed"
    assert response.headers["x-voice-reply-provider"] == "local"


def test_reply_route_passes_configured_provider_to_synthesizer(client, monkeypatch):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    seen: dict = {}

    def fake_synthesize(text, *, provider="none", timeout=30):
        seen["provider"] = provider
        return None

    monkeypatch.setattr("app.api.routes.voice.synthesize", fake_synthesize)
    monkeypatch.setattr(
        "app.api.routes.voice.get_settings",
        lambda: SimpleNamespace(voice_tts_provider="local"))
    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/voice/proposals/{submission.proposal.id}/reply")
    assert response.status_code == 200, response.text
    assert seen["provider"] == "local"
    assert response.json()["provider"] == "local"
