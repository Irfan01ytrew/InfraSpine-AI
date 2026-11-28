"""Voice-first founder mode (Step O): voice note -> transcript -> verified action.

Slice 1 only: deterministic intent parsing, approval-gated proposals, and the
voice-note intake service. Nothing here executes a side effect; execution through
the unified workflow engine is slice 2 (see docs/step-o-voice-plan.md).

Slices 2-3: proposals persist as ``VoiceProposal`` rows, mirror as unified
workflow runs (``origin='voice'``), and execute through ``execute_once`` after
an independent platform approval — see :mod:`app.voice.unified`.
"""

from app.db.voice_models import VoiceProposal
from app.voice.effects import apply_voice_effect, compute_status_overview
from app.voice.intents import VoiceIntent, parse_voice_intent
from app.voice.proposals import (
    VoiceActionProposal,
    VoiceDryRunResult,
    assert_voice_execution_allowed,
    dry_run_voice_action,
    propose_voice_action,
)
from app.voice.records import create_voice_proposal_record, display_status, proposal_out
from app.voice.service import VoiceNoteOutcome, VoiceNoteSubmission, handle_voice_note, submit_voice_note
from app.voice.unified import (
    execute_voice_proposal,
    mirror_pending_voice_proposals,
    mirror_voice_proposal,
    voice_spec,
)

__all__ = [
    "VoiceActionProposal",
    "VoiceDryRunResult",
    "VoiceIntent",
    "VoiceNoteOutcome",
    "VoiceNoteSubmission",
    "VoiceProposal",
    "apply_voice_effect",
    "assert_voice_execution_allowed",
    "compute_status_overview",
    "create_voice_proposal_record",
    "display_status",
    "dry_run_voice_action",
    "execute_voice_proposal",
    "handle_voice_note",
    "mirror_pending_voice_proposals",
    "mirror_voice_proposal",
    "parse_voice_intent",
    "proposal_out",
    "propose_voice_action",
    "submit_voice_note",
    "voice_spec",
]
