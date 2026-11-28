"""Voice-note intake: bounded upload -> transcript -> intent -> proposal (or none).

Reuses the existing bounded upload helper (:func:`app.rag.voice_questions.transcribe_voice_upload`):
audio is staged in a temporary directory, transcribed, and discarded before the
helper returns. This service adds intent parsing and proposal building on top;
it never executes an action and never stores audio.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import AccessGroup, User
from app.db.voice_models import VoiceProposal
from app.platform.access import principal_for
from app.platform.approvals import request_approval
from app.platform.models import PlatformApprovalRequest
from app.platform.modules import group_gate
from app.rag.voice_questions import (
    AudioUpload,
    transcribe_voice_upload,
    voice_question_file_limit,
)
from app.services.audit import AuditContext, record
from app.voice.intents import VoiceIntent, parse_voice_intent
from app.voice.proposals import (
    DEFAULT_VOICE_PROPOSAL_TTL,
    VoiceActionProposal,
    propose_voice_action,
)
from app.voice.records import create_voice_proposal_record
from app.workflows.security import is_manager

VOICE_DUTY = "voice.manage"


@dataclass(frozen=True)
class VoiceNoteOutcome:
    """Result of handling one voice note. ``proposal`` is ``None`` when the note
    asks for nothing recognised — the transcript is still returned so the caller
    can answer it as a question instead."""

    transcript: str
    audio_suffix: str
    byte_count: int
    intent: VoiceIntent
    proposal: VoiceActionProposal | None
    reason: str | None


async def handle_voice_note(
    audio: AudioUpload,
    *,
    scope_id: uuid.UUID,
    requested_by: uuid.UUID,
    max_bytes: int | None = None,
    transcriber: Callable[[Path], str] | None = None,
    temporary_directory: Callable[..., tempfile.TemporaryDirectory] = tempfile.TemporaryDirectory,
    run_in_thread: Callable[..., Awaitable[str]] = asyncio.to_thread,
) -> VoiceNoteOutcome:
    """Transcribe one voice note and propose the action it asks for, if any.

    The transcript is untrusted input: only an allowlisted intent produces a
    proposal, and the proposal still needs a platform approval before anything
    runs (see :mod:`app.voice.proposals`).
    """
    limit = max_bytes if max_bytes is not None else voice_question_file_limit()
    transcript, suffix, byte_count = await transcribe_voice_upload(
        audio,
        max_bytes=limit,
        transcriber=transcriber,
        temporary_directory=temporary_directory,
        run_in_thread=run_in_thread,
    )
    intent = parse_voice_intent(transcript)
    if intent.kind == "unknown":
        return VoiceNoteOutcome(
            transcript=transcript,
            audio_suffix=suffix,
            byte_count=byte_count,
            intent=intent,
            proposal=None,
            reason=intent.reason,
        )
    proposal = propose_voice_action(
        intent,
        scope_id=scope_id,
        requested_by=requested_by,
        transcript=transcript,
    )
    return VoiceNoteOutcome(
        transcript=transcript,
        audio_suffix=suffix,
        byte_count=byte_count,
        intent=intent,
        proposal=proposal,
        reason=None,
    )


@dataclass(frozen=True)
class VoiceNoteSubmission:
    """The stored result of submitting one voice note. ``proposal`` is ``None``
    when the note asked for nothing recognised — nothing is persisted, no
    approval is requested, no workflow run is created."""

    transcript: str
    intent: VoiceIntent
    proposal: VoiceProposal | None
    approval: PlatformApprovalRequest | None
    run_id: uuid.UUID | None
    reason: str | None


async def submit_voice_note(
    db,
    cipher,
    user: User,
    group_id: uuid.UUID,
    audio: AudioUpload,
    *,
    transcriber: Callable[[Path], str] | None = None,
    temporary_directory: Callable[..., tempfile.TemporaryDirectory] = tempfile.TemporaryDirectory,
    run_in_thread: Callable[..., Awaitable[str]] = asyncio.to_thread,
) -> VoiceNoteSubmission:
    """Submit one founder voice note: transcribe -> propose -> persist ->
    mirror as a unified workflow run -> request the platform approval.

    Founder-only: the caller must be a department manager or administrator
    holding ``voice.manage`` on the department (see ``app/platform/sync.py``:
    ``group.manager`` and ``org.admin`` hold it). Anything else is refused
    before the audio is even transcribed.

    Ordering matters: the record and the workflow run are flushed first, and
    the approval request commits them together. If mirroring fails, nothing
    is committed yet, so no orphan approval is left behind; the engine's
    pending sweep mirrors the proposal later.
    """
    group = await db.get(AccessGroup, group_id)
    if group is None or group.kind != "department" or not user.is_active:
        raise NotFoundError("Workflow scope not found.")
    await db.refresh(user)  # auth_version/role must be loaded for principal_for
    manager = await is_manager(db, user, group_id)
    gate = await group_gate(db, group_id, user, legacy_allowed=manager, permission=VOICE_DUTY)
    if not gate.allowed:
        raise PermissionDeniedError(
            "Voice notes need a department manager or administrator.")
    from app.workflows.action_runtime import platform_scope_for
    platform_scope = await platform_scope_for(db, group_id)
    if platform_scope is None:
        raise NotFoundError("Workflow scope not found.")
    outcome = await handle_voice_note(
        audio, scope_id=platform_scope, requested_by=user.id,
        transcriber=transcriber, temporary_directory=temporary_directory,
        run_in_thread=run_in_thread,
    )
    if outcome.proposal is None:
        return VoiceNoteSubmission(
            transcript=outcome.transcript, intent=outcome.intent, proposal=None,
            approval=None, run_id=None, reason=outcome.reason)
    from app.voice.unified import mirror_voice_proposal
    row = await create_voice_proposal_record(
        db, outcome.proposal, group_id=group_id, scope_id=platform_scope)
    record(db, AuditContext(user.id, user.email, None, None), "voice.proposal_created",
           target=str(row.id),
           details={"action_type": row.action_type, "action_hash": row.action_hash,
                    "subject": row.subject, "intent_kind": row.intent_kind})
    run = await mirror_voice_proposal(db, cipher, row)
    approval = await request_approval(
        db, principal_for(user, channel="voice"), outcome.proposal.action_spec,
        required_permission=VOICE_DUTY, ttl=DEFAULT_VOICE_PROPOSAL_TTL)
    row.approval_id = approval.id
    await db.commit()
    return VoiceNoteSubmission(
        transcript=outcome.transcript, intent=outcome.intent, proposal=row,
        approval=approval, run_id=run.id if run is not None else None, reason=None)
