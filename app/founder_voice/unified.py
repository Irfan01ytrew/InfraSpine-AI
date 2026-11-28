"""Founder voice notes on the unified workflow runtime (Step H slice 2).

Every founder voice note gets one workflow run (`origin='founder_voice'`,
`origin_ref=<sha256 of the normalised transcript>`) in the founder's
department. The engine executes it through the `founder_voice.execute` action
handler, so a voice note is listed, audited, paused, stopped and resumed in
the same place as every other process:

* the transcript is **untrusted input**: it is stored sealed in the intake
  record, re-parsed and re-hashed at every step, and can never grant a
  permission, change a payee, or change what an approval covers;
* the spec has an explicit `approval` node (risk `high`) before the action
  node, so the engine waits for one manager decision bound to the exact
  action digest (which includes the transcript hash) before anything runs;
  the requester can never decide their own request (separation of duties);
* each finance execution goes through `execute_once` (key
  `founder_voice:<action_hash>`, duty `finance.read`, read back by
  re-computing the answer), so a crash or a second engine never computes
  the same voice note twice. The duty is `finance.read` on purpose: the
  handler only *reads* the spend chart and the verification headline. The
  duty the request is *about* (`invoice.verify`, `payment.approve`,
  `funds.transfer`) belongs to the person who later performs that duty
  through the finance workflow — not to the voice-note requester, who must
  never gain a duty by asking a question;
* the continuity intent has no platform permission: it is authorized by the
  continuity tenant scope (`scope_for` + `node_for`) inside the handler, and
  a denied node fails the run with `continuity_access_denied` — nothing is
  replaced, nothing is written;
* the approval-request intent records the request only: it never moves money.
  Verify / approve-payment / transfer stay three separate permissions and
  three separate people.

The intake record (`founder_voice_commands`) is the sweep source: a note
whose creation mirror failed is picked up by the engine tick, exactly like
Agent Studio runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import String, and_, select

from app.core.errors import AppError, ConflictError, InvalidInputError, PermissionDeniedError
from app.db.founder_voice_models import FounderVoiceCommandRecord
from app.db.models import User
from app.db.workflow_engine_models import WorkflowRun
from app.platform.approvals import ActionSpec
from app.platform.execution import VerifiedResult, execute_once
from app.qa.founder_voice import (
    FounderVoiceCommand,
    parse_founder_voice_command,
    validate_voice_command,
)
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition
from app.workflows.security import seal, unseal

log = logging.getLogger(__name__)

TEMPLATE_REF = 'founder_voice.commands'
TEMPLATE_NAME = 'Founder voice-note commands'
TEMPLATE_DESCRIPTION = ('Founder voice notes on the unified runtime: one approval bound to the exact '
                        'transcript hash, then a verified read-only answer (spend chart, invoice '
                        'headline, continuity access) or a recorded approval request.')
MIRROR_STATUSES = ('received',)

# Platform permission the *action node* requires and the `execute_once` duty
# uses. The handler only reads, so this is `finance.read` for every intent
# that reads finance data — including the invoice headline and the approval
# request (which only records the request). The duty the request is *about*
# (invoice.verify / payment.approve / funds.transfer, stored on the record)
# belongs to whoever later performs that duty through the finance workflow.
# Continuity has no platform permission: its tenant scope is the authority.
INTENT_EXECUTION_PERMISSION: dict[str, str | None] = {
    'finance_spend_chart': 'finance.read',
    'invoice_verification': 'finance.read',
    'continuity_replacement': None,
    'approval_request': 'finance.read',
    'unknown': None,
}


def founder_voice_spec(intent: str, permission: str | None) -> dict:
    """The system template for one voice-note command. The approval node binds
    the exact transcript hash into its action digest; the action node runs
    only after that approval, as the requester, holding the intent's platform
    permission (when the intent has one)."""
    action_config: dict[str, Any] = {
        'handler': 'founder_voice.execute', 'auto': True,
        'values': {'action_hash': '$input.action_hash', 'intent': '$input.intent',
                   'transcript': '$input.transcript',
                   'batch_id': '$input.batch_id', 'tenant_id': '$input.tenant_id',
                   'node_id': '$input.node_id'},
    }
    if permission:
        action_config['permission'] = permission
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'transcript', 'label': 'Voice-note transcript', 'type': 'text', 'required': True},
                       {'name': 'action_hash', 'label': 'Exact-action hash', 'type': 'text', 'required': True},
                       {'name': 'intent', 'label': 'Intent', 'type': 'text', 'required': True},
                       {'name': 'batch_id', 'label': 'Invoice batch', 'type': 'text', 'required': False},
                       {'name': 'tenant_id', 'label': 'Continuity tenant', 'type': 'text', 'required': False},
                       {'name': 'node_id', 'label': 'Continuity node', 'type': 'text', 'required': False}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Voice note received', 'x': 80, 'y': 160},
            {'id': 'approve', 'type': 'approval', 'label': 'Approval for this exact voice note',
             'x': 340, 'y': 160, 'risk': 'high',
             'config': {'values': {'action_hash': '$input.action_hash', 'intent': '$input.intent'}}},
            {'id': 'execute', 'type': 'action', 'label': 'Founder voice command (verified)',
             'x': 600, 'y': 160, 'risk': 'high', 'timeout_seconds': 600, 'max_attempts': 3,
             'config': action_config},
            {'id': 'end', 'type': 'end', 'label': 'Answer recorded', 'x': 860, 'y': 160},
        ],
        'edges': [{'source': 'start', 'target': 'approve'}, {'source': 'approve', 'target': 'execute'},
                  {'source': 'execute', 'target': 'end'}],
    }).model_dump(mode='json')


def _sealed_transcript(cipher, scope_id, record_id, transcript: str) -> dict:
    return seal(cipher, scope_id, 'founder_voice_transcript', record_id, {'transcript': transcript})


def _open_transcript(cipher, record: FounderVoiceCommandRecord) -> str:
    envelope = {'ciphertext': record.transcript_ciphertext, 'nonce': record.transcript_nonce,
                'key_id': record.transcript_key_id}
    return str(unseal(cipher, record.scope_id, 'founder_voice_transcript', record.id, envelope)['transcript'])


async def record_voice_command(db, cipher, *, owner: User, scope_id, command: FounderVoiceCommand,
                               transcript: str, batch_id=None, tenant_id=None,
                               node_id=None) -> FounderVoiceCommandRecord:
    """Store the intake receipt for one parsed command. Idempotent on the
    action hash: a re-sent voice note returns the same record, never a second
    run. Rejects hostile transcripts and non-commands outright."""
    validation = validate_voice_command(command)
    if validation.status == 'rejected':
        raise InvalidInputError(validation.reason or 'This transcript is rejected.')
    if command.intent == 'unknown':
        raise InvalidInputError('This voice note is not a command I can act on.')
    existing = await db.scalar(select(FounderVoiceCommandRecord).where(
        FounderVoiceCommandRecord.action_hash == command.action_hash))
    if existing is not None:
        if existing.owner_id != owner.id or existing.scope_id != scope_id:
            raise ConflictError('This voice note already belongs to another scope.')
        return existing
    record_id = uuid.uuid4()
    sealed = _sealed_transcript(cipher, scope_id, record_id, transcript)
    permission = command.required_permissions[0] if command.required_permissions else None
    record = FounderVoiceCommandRecord(
        id=record_id, owner_id=owner.id, scope_id=scope_id, intent=command.intent,
        action_hash=command.action_hash, transcript_ciphertext=sealed['ciphertext'],
        transcript_nonce=sealed['nonce'], transcript_key_id=sealed['key_id'],
        required_permission=permission, batch_id=batch_id, tenant_id=tenant_id, node_id=node_id,
        status='received')
    db.add(record)
    await db.flush()
    return record


async def mirror_voice_command(db, cipher, record: FounderVoiceCommandRecord,
                               *, transcript: str | None = None, settings=None) -> WorkflowRun | None:
    """Mirror one intake record to a unified workflow run. The transcript is
    re-parsed and re-hashed here: a record whose transcript no longer matches
    its hash is never mirrored."""
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition, origin_run
    existing = await origin_run(db, 'founder_voice', record.action_hash)
    if existing is not None:
        return existing
    owner = await db.get(User, record.owner_id)
    if owner is None or not owner.is_active:
        return None
    if transcript is None:
        transcript = _open_transcript(cipher, record)
    command = parse_founder_voice_command(transcript)
    if command.action_hash != record.action_hash or command.intent != record.intent:
        record.status, record.failure_code = 'failed', 'transcript_hash_mismatch'
        return None
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    definition = await ensure_system_definition(
        db, scope_id=record.scope_id, owner_id=owner.id, origin='founder_voice', origin_ref=TEMPLATE_REF,
        name=TEMPLATE_NAME, description=TEMPLATE_DESCRIPTION,
        spec=founder_voice_spec(record.intent, INTENT_EXECUTION_PERMISSION.get(record.intent)))
    if not definition.enabled:
        return None  # a department manager turned the unified path off
    service = WorkflowService(db, owner, cipher, AuditContext(owner.id, owner.email, None, None))
    inputs = {'transcript': command.action_text, 'action_hash': command.action_hash, 'intent': command.intent,
              'batch_id': str(record.batch_id) if record.batch_id else '',
              'tenant_id': str(record.tenant_id) if record.tenant_id else '',
              'node_id': str(record.node_id) if record.node_id else ''}
    run = await service.trigger(record.scope_id, definition.id, inputs, f'founder-voice:{record.action_hash}',
                                origin='founder_voice', origin_ref=record.action_hash)
    record.status = 'mirrored'
    return run


async def mirror_pending_voice_commands(sessionmaker, cipher, *, limit: int = 25) -> int:
    """Engine-tick sweep: mirror intake records whose creation mirror failed."""
    count = 0
    async with sessionmaker() as db:
        query = (select(FounderVoiceCommandRecord)
                 .where(FounderVoiceCommandRecord.status.in_(MIRROR_STATUSES))
                 .order_by(FounderVoiceCommandRecord.created_at).limit(limit))
        for record in (await db.scalars(query)).all():
            try:
                async with db.begin_nested():
                    if await mirror_voice_command(db, cipher, record) is not None:
                        count += 1
            except AppError as exc:
                record.status, record.failure_code = 'failed', exc.code
                log.info('Founder voice command %s not mirrored (%s)', record.id, exc.code)
        await db.commit()
    return count


async def on_workflow_cancelled(db, cipher, run: WorkflowRun) -> None:
    """Stopping the workflow run cancels the intake record. A run that already
    executed stays executed; the record is only a receipt."""
    record = await db.scalar(select(FounderVoiceCommandRecord).where(
        FounderVoiceCommandRecord.action_hash == str(run.origin_ref)).with_for_update())
    if record is None or record.status != 'mirrored':
        return
    record.status = 'cancelled'


def _attempt_spec(ctx: ActionContext, command: FounderVoiceCommand) -> ActionSpec:
    values = ctx.values
    return ActionSpec('founder_voice.execute', ctx.platform_scope_id,
                      {'action_hash': command.action_hash, 'intent': command.intent,
                       'batch_id': str(values.get('batch_id') or ''),
                       'tenant_id': str(values.get('tenant_id') or ''),
                       'node_id': str(values.get('node_id') or '')},
                      subject=f'founder_voice:{command.action_hash}')


class FounderVoiceExecuteHandler:
    name = 'founder_voice.execute'
    label = 'Founder voice command (read-only answer, verified by read-back)'
    module = '12'
    permission = None  # the node config carries the intent's permission; execute_once checks the duty

    async def _load(self, ctx: ActionContext) -> tuple[FounderVoiceCommand, FounderVoiceCommandRecord] | None:
        try:
            action_hash = str(ctx.values['action_hash'])
            intent = str(ctx.values['intent'])
            transcript = str(ctx.values['transcript'])
            command = parse_founder_voice_command(transcript)
        except (KeyError, TypeError, ValueError):
            return None
        if command.action_hash != action_hash or command.intent != intent:
            return None  # the transcript changed after approval: never execute a different action
        if validate_voice_command(command).status == 'rejected':
            return None
        record = await ctx.db.scalar(select(FounderVoiceCommandRecord).where(
            FounderVoiceCommandRecord.action_hash == action_hash))
        # Owner binding: a workflow can only carry the requester's own voice note.
        if record is None or record.owner_id != ctx.principal.user_id:
            return None
        return command, record

    async def _compute(self, ctx: ActionContext, command: FounderVoiceCommand,
                       record: FounderVoiceCommandRecord) -> dict | None:
        """The read-only answer for one intent. Pure reads; no writes, no
        money movement, no permission grants. Returns None only for a denied
        continuity replacement."""
        if command.intent == 'finance_spend_chart':
            return await self._spend_chart(ctx)
        if command.intent == 'invoice_verification':
            return await self._verification_headline(ctx, record)
        if command.intent == 'continuity_replacement':
            return await self._continuity_access(ctx, record)
        if command.intent == 'approval_request':
            return {'intent': command.intent, 'approval': 'requested',
                    'action_hash': command.action_hash,
                    'required_permission': command.required_permissions[0]
                    if command.required_permissions else None}
        raise InvalidInputError('This voice note is not a command I can act on.')

    async def _spend_chart(self, ctx: ActionContext) -> dict:
        """Monthly spend by currency over verified/approved/paid invoices in
        this department. Read-only aggregation; amounts render as strings."""
        from sqlalchemy import func

        from app.db.finance_models import FinInvoice
        rows = (await ctx.db.execute(
            select(func.to_char(FinInvoice.invoice_date, 'YYYY-MM'), FinInvoice.currency,
                   func.coalesce(func.sum(FinInvoice.amount), 0))
            .where(FinInvoice.group_id == ctx.group_id,
                   FinInvoice.status.in_(('verified', 'approved', 'paid')),
                   FinInvoice.invoice_date.is_not(None))
            .group_by(func.to_char(FinInvoice.invoice_date, 'YYYY-MM'), FinInvoice.currency)
            .order_by(func.to_char(FinInvoice.invoice_date, 'YYYY-MM'), FinInvoice.currency)
        )).all()
        series = [{'month': month, 'currency': currency, 'total': str(total)}
                  for month, currency, total in rows]
        return {'intent': 'finance_spend_chart',
                'chart': {'title': 'Spend by month', 'series': series}}

    async def _verification_headline(self, ctx: ActionContext, record: FounderVoiceCommandRecord) -> dict:
        """The '32 of 50 verified' headline for the command's invoice batch
        (or the latest batch in this department)."""
        from uuid import UUID as _UUID

        from app.core.config import get_settings
        from app.db.finance_models import FinInvoiceBatch
        from app.finance.service import FinanceService
        batch = None
        if record.batch_id is not None:
            batch = await ctx.db.get(FinInvoiceBatch, record.batch_id)
        if batch is None:
            batch = await ctx.db.scalar(select(FinInvoiceBatch).where(
                FinInvoiceBatch.group_id == ctx.group_id).order_by(FinInvoiceBatch.created_at.desc()).limit(1))
        if batch is None:
            raise InvalidInputError('No invoice batch exists in this department yet.')
        user = await ctx.db.get(User, ctx.principal.user_id)
        service = FinanceService(ctx.db, user, get_settings(), ctx.cipher)
        summary = await service.summary(batch)
        return {'intent': 'invoice_verification', 'batch_id': str(batch.id),
                'headline': summary['headline'], 'total': summary['total'],
                'verified': summary['verified'], 'by_status': summary['by_status']}

    async def _continuity_access(self, ctx: ActionContext, record: FounderVoiceCommandRecord) -> dict | None:
        """Is the requester allowed to replace this continuity node? The
        continuity tenant scope decides. Returns None when denied — the
        caller turns that into a failed step, and nothing is replaced."""
        from app.continuity.access import node_for, scope_for
        from app.core.errors import NotFoundError
        user = await ctx.db.get(User, ctx.principal.user_id)
        if record.tenant_id is None or record.node_id is None:
            raise InvalidInputError('This voice note names no continuity node.')
        try:
            scope = await scope_for(ctx.db, record.tenant_id, user)
            node = await node_for(ctx.db, scope, record.node_id)
        except NotFoundError:
            return None
        return {'intent': 'continuity_replacement', 'continuity_access': 'allowed',
                'node_id': str(node.id), 'department_id': str(node.department_id)}

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        loaded = await self._load(ctx)
        if loaded is None:
            return ActionOutcome('failed', failure_code='founder_voice_command_missing')
        command, record = loaded
        if command.intent == 'continuity_replacement':
            # No platform duty exists for continuity: the tenant scope is the
            # authority, and the check is a pure read verified again below.
            output = await self._compute(ctx, command, record)
            if output is None:
                return ActionOutcome('failed', {'intent': command.intent, 'continuity_access': 'denied'},
                                     'continuity_access_denied')
            ctx.note('founder_voice.executed', intent=command.intent, action_hash=command.action_hash,
                     continuity_access='allowed')
            return ActionOutcome('done', output)
        if command.intent == 'approval_request':
            # Records the request only. Money never moves here; the engine's
            # approval node already bound the decision to this exact action.
            output = await self._compute(ctx, command, record)
            ctx.note('founder_voice.executed', intent=command.intent, action_hash=command.action_hash)
            return ActionOutcome('done', output)
        permission = command.required_permissions[0] if command.required_permissions else None
        if permission is None:
            return ActionOutcome('failed', failure_code='founder_voice_no_duty')
        spec = _attempt_spec(ctx, command)
        key = f'founder_voice:{command.action_hash}'

        async def dispatch() -> str:
            output = await self._compute(ctx, command, record)
            return json.dumps(output, sort_keys=True, ensure_ascii=False, default=str)

        async def verify(ref: str) -> VerifiedResult:
            recomputed = await self._compute(ctx, command, record)
            current = json.dumps(recomputed, sort_keys=True, ensure_ascii=False, default=str)
            return VerifiedResult(current == ref, {'action_hash': command.action_hash,
                                                   'ref_matches': current == ref})

        result = await execute_once(ctx.db, ctx.principal, key, spec, duty=permission,
                                    dispatch=dispatch, verify=verify)
        if result.status == 'claimed' and result.replayed:
            return ActionOutcome('waiting', {'intent': command.intent, 'action_hash': command.action_hash},
                                 poll_seconds=5)
        if result.status == 'failed':
            return ActionOutcome('failed', {'intent': command.intent, 'action_hash': command.action_hash},
                                 'founder_voice_execution_failed')
        output = await self._compute(ctx, command, record)
        ctx.note('founder_voice.executed', intent=command.intent, action_hash=command.action_hash,
                 execution=result.status)
        return ActionOutcome('done', output)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        loaded = await self._load(ctx)
        if loaded is None:
            return VerifiedResult(False, {'reason': 'command_missing'})
        command, record = loaded
        try:
            current = await self._compute(ctx, command, record)
        except AppError as exc:
            # A continuity denial at verify time means the access changed
            # between run and verify: the step must not complete.
            return VerifiedResult(False, {'reason': exc.code})
        if command.intent == 'approval_request':
            ok = output.get('action_hash') == command.action_hash and output.get('approval') == 'requested'
            return VerifiedResult(ok, {'action_hash': command.action_hash})
        if command.intent == 'continuity_replacement':
            if current is None:
                return VerifiedResult(False, {'reason': 'continuity_access_denied'})
            ok = (output.get('continuity_access') == 'allowed'
                  and current.get('continuity_access') == 'allowed'
                  and output.get('node_id') == current.get('node_id'))
            return VerifiedResult(ok, {'node_id': output.get('node_id')})
        ok = output == current
        return VerifiedResult(ok, {'action_hash': command.action_hash})


register(FounderVoiceExecuteHandler())
