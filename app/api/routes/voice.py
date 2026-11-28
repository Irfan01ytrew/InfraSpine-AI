"""Voice-first founder mode (Step O, slices 2-3): submit a voice note, watch its proposal.

* ``POST /api/voice/note`` — founder/manager only (``voice.manage``): upload a
  voice note for a department, get back the stored proposal, the platform
  approval that must be decided by someone else, and the unified workflow run
  carrying it. A note that asks for nothing recognised returns no proposal.
* ``GET /api/voice/proposals/{id}`` — the proposal's status, derived from its
  platform approval until execution writes the outcome.
* ``GET /api/voice/proposals?group_id=...`` — the caller's proposals in a
  department (managers see everyone's).

Approval decisions themselves go through the generic inbox,
``POST /api/approvals/{id}/decide`` (exact-action hash, separation of duties);
the workflow's approval step polls that decision.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.errors import NotFoundError, PermissionDeniedError
from app.voice.records import (
    approval_status,
    display_status,
    load_proposal,
    proposal_out,
    proposals_for_group,
)
from app.voice.reply import build_voice_reply
from app.voice.service import submit_voice_note
from app.voice.tts import synthesize
from app.workflows.security import is_manager, require_scope

router = APIRouter(prefix="/api/voice", tags=["voice"])


@router.post("/note", status_code=201)
async def submit_note(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    group_id: Annotated[UUID, Form()],
    audio: Annotated[UploadFile, File(description="Short voice note; transcribed locally and discarded")],
):
    """Submit a founder voice note for a department.

    Returns the transcript, the recognised intent, and — when the note asks
    for a recognised action — the stored proposal, the approval to decide, and
    the workflow run. A note with no recognised intent returns
    ``proposal: null`` with the reason; nothing is stored.
    """
    submission = await submit_voice_note(db, request.app.state.cipher, user, group_id, audio)
    body = {
        "transcript": submission.transcript,
        "intent_kind": submission.intent.kind,
        "reason": submission.reason,
        "proposal": None,
        "approval": None,
        "wf_run_id": None,
    }
    if submission.proposal is not None:
        body["proposal"] = await proposal_out(db, submission.proposal)
        body["wf_run_id"] = str(submission.run_id) if submission.run_id else None
        if submission.approval is not None:
            body["approval"] = {
                "id": str(submission.approval.id),
                "action_hash": submission.approval.action_hash,
                "required_permission": submission.approval.required_permission,
                "expires_at": submission.approval.expires_at.isoformat(),
                "status": submission.approval.status,
            }
    return body


async def _visible_proposal(db, user, proposal_id: UUID):
    """A proposal the caller may see: a member of its department, and either
    the requester or a department manager. Anything else is 404 (the proposal
    is not revealed)."""
    row = await load_proposal(db, proposal_id)
    if row is None:
        raise NotFoundError("Voice proposal not found.")
    await require_scope(db, user, row.group_id)
    if row.requested_by != user.id and not await is_manager(db, user, row.group_id):
        raise NotFoundError("Voice proposal not found.")
    return row


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: UUID, db: DbSession, user: CurrentUser):
    row = await _visible_proposal(db, user, proposal_id)
    return await proposal_out(db, row)


@router.get("/proposals/{proposal_id}/reply")
async def get_reply(proposal_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    """Spoken reply for one proposal (Step O, slice 5).

    The reply text is built deterministically from the proposal's stored
    state. When the configured TTS provider synthesizes audio, the response
    is the audio (``audio/wav``) with the text in the ``X-Voice-Reply-Text``
    header; otherwise it is JSON with the text and ``audio_available: false``
    — the text fallback, never an error. Same visibility as the proposal
    itself (requester or department manager, 404 otherwise).
    """
    row = await _visible_proposal(db, user, proposal_id)
    approval = await approval_status(db, row.approval_id)
    status = display_status(row, approval)
    reply = build_voice_reply(
        intent_kind=row.intent_kind,
        status=status,
        execution_result=row.execution_result,
        failure_reason=row.failure_reason,
    )
    provider = get_settings().voice_tts_provider
    audio = synthesize(reply.text, provider=provider)
    if audio is None:
        return {
            "text": reply.text,
            "status": status,
            "audio_available": False,
            "reason": "tts_unavailable",
            "provider": provider,
        }
    return Response(
        content=audio.audio,
        media_type=audio.content_type,
        headers={
            "X-Voice-Reply-Text": reply.text,
            "X-Voice-Reply-Status": status,
            "X-Voice-Reply-Provider": audio.provider,
        },
    )


@router.get("/proposals")
async def list_proposals(group_id: UUID, db: DbSession, user: CurrentUser):
    await require_scope(db, user, group_id)
    rows = await proposals_for_group(db, group_id)
    if await is_manager(db, user, group_id):
        visible = rows
    else:
        visible = [row for row in rows if row.requested_by == user.id]
    return {"proposals": [await proposal_out(db, row) for row in visible]}
