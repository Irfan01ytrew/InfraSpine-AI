"""Step H slices 2 + 4: founder voice-note intake (founder-only).

A founder voice note is a request, never an order: the transcript is parsed
by `app/qa/founder_voice.py`, stored sealed as untrusted input, mirrored to
one unified workflow run (`origin='founder_voice'`), and executed only after
a manager approval bound to the exact transcript hash.

Founder-only means: the caller holds the admin role (the company owner) and
belongs to the target department. A non-admin founder, or a founder from
another department, gets 403 / 404 — the voice note is never recorded for
them. Audio uploads reuse the voice-question transcription path (local
Whisper first, same 8 MB / 120 s caps; staged audio is discarded).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, DbSession, audit_context
from app.founder_voice.unified import mirror_voice_command, record_voice_command
from app.qa.founder_voice import parse_founder_voice_command
from app.rag.voice_questions import transcribe_voice_upload, voice_question_file_limit
from app.services.audit import record as audit_record
from app.workflows.security import require_scope
from app.workflows.unified import origin_run

router = APIRouter(prefix="/api/founder-voice", tags=["founder-voice"])


class FounderVoiceNoteIn(BaseModel):
    scope_id: uuid.UUID
    transcript: str = Field(min_length=1, max_length=4000)
    batch_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None


class FounderVoiceNoteOut(BaseModel):
    command_id: str
    intent: str
    action_hash: str
    requires_approval: bool
    workflow_run_id: str | None
    status: str


async def _intake(db, cipher, user, *, scope_id, transcript, batch_id=None,
                  tenant_id=None, node_id=None) -> FounderVoiceNoteOut:
    await require_scope(db, user, scope_id)
    command = parse_founder_voice_command(transcript)
    record = await record_voice_command(db, cipher, owner=user, scope_id=scope_id, command=command,
                                        transcript=transcript, batch_id=batch_id,
                                        tenant_id=tenant_id, node_id=node_id)
    run = await mirror_voice_command(db, cipher, record, transcript=transcript)
    await db.commit()
    return FounderVoiceNoteOut(command_id=str(record.id), intent=command.intent,
                               action_hash=command.action_hash, requires_approval=True,
                               workflow_run_id=str(run.id) if run is not None else None,
                               status=record.status)


@router.post("/note", response_model=FounderVoiceNoteOut, status_code=201)
async def founder_voice_note(request: Request, db: DbSession, user: AdminUser,
                             payload: FounderVoiceNoteIn) -> FounderVoiceNoteOut:
    """Intake one founder voice note from its transcript (mocked transcription
    in tests; the audio route below transcribes for real)."""
    out = await _intake(db, request.app.state.cipher, user, scope_id=payload.scope_id,
                        transcript=payload.transcript, batch_id=payload.batch_id,
                        tenant_id=payload.tenant_id, node_id=payload.node_id)
    audit_record(db, audit_context(request, user), 'founder_voice.note_received',
                 target=out.command_id, details={'intent': out.intent, 'action_hash': out.action_hash})
    await db.commit()
    return out


@router.post("/note-audio", response_model=FounderVoiceNoteOut, status_code=201)
async def founder_voice_note_audio(
    request: Request, db: DbSession, user: AdminUser,
    audio: Annotated[UploadFile, File(description="Short founder voice note; transcribed locally and discarded")],
    scope_id: Annotated[uuid.UUID, Form()],
    batch_id: Annotated[str | None, Form()] = None,
    tenant_id: Annotated[str | None, Form()] = None,
    node_id: Annotated[str | None, Form()] = None,
) -> FounderVoiceNoteOut:
    """Intake one founder voice note from an audio upload. The staged audio is
    discarded by the transcription helper; only the sealed transcript is kept."""
    transcript, _, _ = await transcribe_voice_upload(audio, max_bytes=voice_question_file_limit())
    out = await _intake(db, request.app.state.cipher, user, scope_id=scope_id, transcript=transcript,
                        batch_id=uuid.UUID(batch_id) if batch_id else None,
                        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
                        node_id=uuid.UUID(node_id) if node_id else None)
    audit_record(db, audit_context(request, user), 'founder_voice.note_received',
                 target=out.command_id, details={'intent': out.intent, 'action_hash': out.action_hash})
    await db.commit()
    return out


@router.get("/commands/{action_hash}", response_model=FounderVoiceNoteOut)
async def founder_voice_command(request: Request, db: DbSession, user: AdminUser,
                                action_hash: str) -> FounderVoiceNoteOut:
    """Read one intake record and its mirrored run (founder-only, read-only)."""
    from app.db.founder_voice_models import FounderVoiceCommandRecord
    from sqlalchemy import select
    record = await db.scalar(select(FounderVoiceCommandRecord).where(
        FounderVoiceCommandRecord.action_hash == action_hash))
    if record is None:
        from app.core.errors import NotFoundError
        raise NotFoundError('Voice note not found.')
    await require_scope(db, user, record.scope_id)
    run = await origin_run(db, 'founder_voice', action_hash)
    return FounderVoiceNoteOut(command_id=str(record.id), intent=record.intent,
                               action_hash=record.action_hash, requires_approval=True,
                               workflow_run_id=str(run.id) if run is not None else None,
                               status=record.status)
