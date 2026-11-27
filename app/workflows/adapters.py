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


def _sessionmaker_for(db):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    return async_sessionmaker(db.bind, expire_on_commit=False)


class AutomationRunAdapter:
    """Legacy automation rules on the unified runtime. A legacy run (created by the automation API, which is kept
    as a facade) is tracked and driven by a workflow step; the action itself runs through the one shared, idempotent
    executor (`automation_worker.process_workflow_run`), so a task or message is never created twice."""

    name = 'automation.run'
    label = 'Automation rule (legacy facade)'
    module = '05'
    allowed_risks = {'low', 'medium', 'high', 'critical'}
    idempotent = True

    async def _run(self, db, scope_id, values):
        from uuid import UUID
        from app.db.models import AutomationRun
        try:
            run_id = UUID(str(values.get('automation_run_id')))
        except ValueError:
            raise InvalidInputError('Automation run reference is invalid.') from None
        run = await db.get(AutomationRun, run_id, populate_existing=True)
        if run is None or run.department_group_id != scope_id:
            raise PermissionDeniedError('Automation run is outside the workflow scope.')
        return run

    async def authorize(self, db, actor, scope_id, resource_id, values):
        await require_scope(db, actor, scope_id)
        if values.get('automation_run_id'):
            await self._run(db, scope_id, values)

    async def execute(self, db, cipher, actor, scope_id, dispatch, payload):
        values = payload['values']
        await self.authorize(db, actor, scope_id, None, values)
        run = await self._run(db, scope_id, values)
        if run.status == 'queued':
            from app.services.automation_worker import process_workflow_run
            await process_workflow_run(_sessionmaker_for(db), run.id)
            run = await self._run(db, scope_id, values)
        receipt = {'automation_run_id': str(run.id), 'status': run.status}
        if run.status == 'completed':
            return AdapterResult('completed', receipt | {'completed_at': run.completed_at.isoformat(), 'verified': True})
        if run.status in {'failed', 'rejected', 'cancelled'}:
            return AdapterResult('failed', receipt, f'automation_{run.status}')
        return AdapterResult('pending', receipt)


class AgentStudioRunAdapter:
    """`agent_step`: an Agent Studio agent run as one workflow step. The run is queued under the workflow requester
    (who must own the agent and thread); its own tool approvals still apply. Completed means the run finished."""

    name = 'agent_studio.run'
    label = 'Agent Studio agent run (agent step)'
    module = 'agent-studio'
    allowed_risks = {'low', 'medium', 'high', 'critical'}
    idempotent = True

    async def authorize(self, db, actor, scope_id, resource_id, values):
        from uuid import UUID
        from app.agent_studio.service import _require_agent
        await require_scope(db, actor, scope_id)
        try:
            agent_id = UUID(str(values.get('agent_id')))
        except ValueError:
            raise InvalidInputError('Select an Agent Studio agent.') from None
        return await _require_agent(db, actor, agent_id, enabled=True)

    async def execute(self, db, cipher, actor, scope_id, dispatch, payload):
        from uuid import UUID
        from app.agent_studio.service import AgentStudioService
        from app.core.config import get_settings
        values = payload['values']
        agent = await self.authorize(db, actor, scope_id, None, values)
        try:
            thread_id = UUID(str(values.get('thread_id')))
        except ValueError:
            raise InvalidInputError('Select an Agent Studio thread.') from None
        prompt = values.get('prompt') or payload['node']['config'].get('instructions', '')
        service = AgentStudioService(db, actor, get_settings(), cipher, None, None)
        run, _, _ = await service.start_run(agent.id, thread_id, str(prompt), idempotency_key=f'wf:{dispatch.id}')
        receipt = {'agent_run_id': str(run.id), 'status': run.status}
        if run.status == 'completed':
            return AdapterResult('completed', receipt | {'verified': True})
        if run.status in {'failed', 'cancelled'}:
            return AdapterResult('failed', receipt, f'agent_{run.status}')
        return AdapterResult('pending', receipt)


class GovernanceActionAdapter:
    """Execute an approved governance action request (module 03 tool policy) as a workflow step."""

    name = 'governance.execute'
    label = 'Approved governance action'
    module = '03'
    allowed_risks = {'medium', 'high', 'critical'}
    idempotent = True

    async def authorize(self, db, actor, scope_id, resource_id, values):
        await require_scope(db, actor, scope_id)
        for key in ('tenant_id', 'request_id'):
            if not values.get(key):
                raise InvalidInputError('Governance tenant and action request are required.')

    async def execute(self, db, cipher, actor, scope_id, dispatch, payload):
        from uuid import UUID
        from app.governance import actions as governance_actions
        values = payload['values']
        await self.authorize(db, actor, scope_id, None, values)
        request = await governance_actions.execute(db, actor, UUID(str(values['tenant_id'])), UUID(str(values['request_id'])))
        receipt = {'governance_request_id': str(request.id), 'status': request.status, 'action_hash': request.action_hash}
        if request.status == 'executed':
            return AdapterResult('completed', receipt | {'verified': True})
        return AdapterResult('failed', receipt, f'governance_{request.status}')


ADAPTERS = {adapter.name: adapter for adapter in (
    AgentReadAdapter(), AutomationRunAdapter(), AgentStudioRunAdapter(), GovernanceActionAdapter())}


def catalog():
    return [{'name': a.name, 'label': a.label, 'module': a.module,
             'allowed_risks': sorted(a.allowed_risks), 'idempotent': a.idempotent} for a in ADAPTERS.values()]
