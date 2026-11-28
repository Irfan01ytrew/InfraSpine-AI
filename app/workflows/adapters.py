"""Module-owned action contracts. Every adapter reauthorizes at dispatch and poll.

Adapters must stage effects in the passed transaction or implement their own
idempotent inbox using dispatch.id. Returning a receipt means verified completion,
not transport acceptance. Unknown outcomes must never be blindly retried.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import json

from app.core.errors import ConflictError, InvalidInputError, PermissionDeniedError
from app.db.models import AgentProfileRun
from app.services import agents
from app.workflows.definition import digest
from app.workflows.security import require_scope


@dataclass(frozen=True)
class AdapterResult:
    status: str  # pending | completed | failed | unknown
    receipt: dict
    failure_code: str | None = None


class AgentReadAdapter:
    name = 'irfanix.read'
    label = 'IRFANIX authorized read-only specialist'
    module = '02'
    allowed_risks = {'medium', 'high', 'critical'}
    idempotent = True

    async def authorize(self, db, actor, scope_id, resource_id, values):
        await require_scope(db, actor, scope_id)
        capability = values.get('capability', 'knowledge_search')
        if capability not in {'knowledge_search', 'company_data_analysis'}:
            raise InvalidInputError('Only authorized read-only specialist capabilities are supported.')
        if resource_id is None: raise InvalidInputError('Select an authorized agent profile.')
        profile = await agents.profile_for_run(db, actor, resource_id, capability)
        if profile.department_group_id != scope_id:
            raise PermissionDeniedError('Agent profile is outside the workflow scope.')
        return profile

    async def execute(self, db, cipher, actor, scope_id, dispatch, payload):
        from uuid import UUID
        values = payload['values']
        resource_id = UUID(payload['node']['config']['resource_id'])
        profile = await self.authorize(db, actor, scope_id, resource_id, values)
        existing = await db.get(AgentProfileRun, dispatch.id)
        if existing is None:
            task = values.get('task', payload['node']['config'].get('instructions', ''))
            if not isinstance(task, str) or not 1 <= len(task.strip()) <= 8000:
                raise InvalidInputError('Agent task must contain 1–8000 characters.')
            documents = values.get('document_ids', [])
            if not isinstance(documents, list) or len(documents) > 50:
                raise InvalidInputError('Agent document references are invalid.')
            documents = [str(UUID(value)) for value in documents]
            encrypted = agents.seal_run_value(cipher, dispatch.id, 'task', json.dumps({'task': task, 'document_ids': documents}))
            existing = AgentProfileRun(id=dispatch.id, profile_id=profile.id, profile_version=profile.version,
                department_group_id=scope_id, requester_user_id=actor.id,
                capability=values.get('capability', 'knowledge_search'), task_ciphertext=encrypted.ciphertext,
                task_nonce=encrypted.nonce, task_key_id=encrypted.key_id, status='queued', execution_mode='queue',
                attempts=0, requested_at=datetime.now(UTC), available_at=datetime.now(UTC), started_at=None)
            db.add(existing)
            await db.flush()
        if (existing.requester_user_id != actor.id or existing.department_group_id != scope_id
                or existing.profile_id != profile.id or existing.profile_version != profile.version):
            raise ConflictError('Agent receipt binding no longer matches the authorized action.')
        if existing.status == 'failed': return AdapterResult('failed', {'agent_run_id': str(existing.id)}, 'agent_failed')
        if existing.status != 'completed': return AdapterResult('pending', {'agent_run_id': str(existing.id)})
        if existing.result_ciphertext is None:
            return AdapterResult('failed', {'agent_run_id': str(existing.id)}, 'agent_result_unavailable')
        result = json.loads(agents.open_run_value(cipher, existing, 'result'))
        if result.get('insufficient_evidence'):
            return AdapterResult('failed', {'agent_run_id': str(existing.id), 'result_digest': digest(result)}, 'agent_verification_failed')
        return AdapterResult('completed', {'agent_run_id': str(existing.id), 'result_digest': digest(result),
                                          'verified': True, 'result': result})


ADAPTERS = {adapter.name: adapter for adapter in (AgentReadAdapter(),)}


def catalog():
    return [{'name': a.name, 'label': a.label, 'module': a.module,
             'allowed_risks': sorted(a.allowed_risks), 'idempotent': a.idempotent} for a in ADAPTERS.values()]
