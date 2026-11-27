"""Action-bound human decisions and transactional tool adapters.

This gateway never runs model-supplied code, URLs, SQL, paths or arbitrary tools.
Adapters must resolve source authorization again at execution, in this transaction.
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import func, select, text

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import Document, User
from app.governance import models as m
from app.governance.policy import evaluate
from app.governance.service import context, event, grants, now, scoped
from app.rag.access import can_modify, can_publish, readable_by


def fingerprint(request, resource):
    values = {key: str(getattr(request, key)) for key in (
        "tenant_id", "requester_user_id", "resource_id", "agent_id", "action", "policy_version", "risk", "required_approvals", "expires_at",
    )}
    values["resource"] = {key: str(getattr(resource, key)) for key in ("kind", "external_id", "department_id", "classification")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def source(db, actor, resource, action, *, lock=False):
    if resource.kind == "document":
        stmt = select(Document).where(Document.id == resource.external_id, readable_by(actor))
        if lock:
            stmt = stmt.with_for_update()
        doc = await db.scalar(stmt.execution_options(populate_existing=True))
        if doc is None:
            raise NotFoundError()
        if action == "document.share" and (not can_modify(actor, doc) or not can_publish(actor, "shared")):
            raise PermissionDeniedError("Source publication permission is required.")
        return doc
    return await scoped(db, m.AgentIdentity, resource.tenant_id, resource.external_id)


async def authorize(db, actor, tenant_id, payload, *, version=None):
    tenant, member, roles, permissions = await context(db, actor, tenant_id)
    resource = await scoped(db, m.Resource, tenant_id, payload.resource_id)
    policy = await db.get(m.PolicyVersion, (tenant_id, version or tenant.policy_version))
    result = evaluate(policy.rules if policy else [], action=payload.action, resource=resource,
                      roles=roles, permissions=permissions, department_id=member.department_id, all_departments=member.all_departments)
    if payload.agent_id:
        agent = await scoped(db, m.AgentIdentity, tenant_id, payload.agent_id)
        if not agent.enabled or agent.owner_user_id != actor.id or payload.action not in agent.tools:
            from app.governance.policy import Evaluation
            result = Evaluation("deny", result.risk, 0, "agent_disabled_owner_or_tool_scope", [], [])
    return tenant, resource, result


async def request_action(db, actor, tenant_id, payload):
    await context(db, actor, tenant_id, lock=True)
    tenant, resource, evaluation = await authorize(db, actor, tenant_id, payload)
    if evaluation.effect == "deny":
        await event(db, tenant_id, actor, "tool_denied", outcome="failure", target=resource.id, details={"reason": evaluation.reason, "action": payload.action})
        await db.commit()
        raise PermissionDeniedError("Policy denied this action: " + evaluation.reason)
    await source(db, actor, resource, payload.action)
    row = m.ActionRequest(
        tenant_id=tenant_id, requester_user_id=actor.id, resource_id=resource.id,
        agent_id=payload.agent_id, action=payload.action, policy_version=tenant.policy_version,
        risk=evaluation.risk, required_approvals=evaluation.required_approvals,
        status="pending" if evaluation.required_approvals else "ready", expires_at=now() + timedelta(hours=1),
    )
    row.action_hash = fingerprint(row, resource)
    db.add(row)
    await db.flush()
    await event(db, tenant_id, actor, "action_requested", target=row.id, details={"action": row.action, "risk": row.risk, "action_hash": row.action_hash})
    await db.commit()
    return row


async def valid_approvals(db, request, resource):
    votes = (await db.execute(select(m.Decision).where(m.Decision.tenant_id == request.tenant_id, m.Decision.request_id == request.id, m.Decision.decision == "approve"))).scalars().all()
    valid = 0
    for vote in votes:
        member = await db.scalar(select(m.Member).join(User, User.id == m.Member.user_id).where(
            m.Member.tenant_id == request.tenant_id, m.Member.user_id == vote.actor_id,
            m.Member.active.is_(True), User.is_active.is_(True),
        ))
        if member is None or (member.expires_at and member.expires_at <= now()):
            continue
        if resource.department_id and not member.all_departments and member.department_id != resource.department_id:
            continue
        if request.risk == "medium":
            valid += int(vote.actor_id == request.requester_user_id)
        elif vote.actor_id != request.requester_user_id:
            _, permissions = await grants(db, request.tenant_id, vote.actor_id)
            valid += int("approval.decide" in permissions)
    return valid


async def decide(db, actor, tenant_id, request_id, payload):
    tenant, member, _, permissions = await context(db, actor, tenant_id, lock=True)
    request = await scoped(db, m.ActionRequest, tenant_id, request_id)
    resource = await scoped(db, m.Resource, tenant_id, request.resource_id)
    if resource.department_id and not member.all_departments and member.department_id != resource.department_id:
        raise PermissionDeniedError()
    if request.risk == "medium":
        if request.requester_user_id != actor.id:
            raise PermissionDeniedError("This action requires its requester's explicit confirmation.")
    elif "approval.decide" not in permissions or request.requester_user_id == actor.id:
        raise PermissionDeniedError("An independent authorized reviewer is required.")
    if request.status not in ("pending", "ready") or request.required_approvals == 0:
        raise ConflictError("This request is not awaiting a decision.")
    if request.expires_at <= now() or request.policy_version != tenant.policy_version:
        raise ConflictError("The request expired or its policy changed; request a new action.")
    if request.action_hash != fingerprint(request, resource):
        raise ConflictError("The saved action does not match its approval binding.")
    if await db.get(m.Decision, (tenant_id, request_id, actor.id)):
        raise ConflictError("This identity has already decided; independent reviewers are required.")
    db.add(m.Decision(tenant_id=tenant_id, request_id=request_id, actor_id=actor.id, **payload.model_dump()))
    await db.flush()
    if payload.decision == "reject":
        request.status = "rejected"
    elif await valid_approvals(db, request, resource) >= request.required_approvals:
        request.status = "ready"
    await event(db, tenant_id, actor, "action_" + payload.decision, target=request.id, details={"action_hash": request.action_hash})
    await db.commit()
    return request


async def execute(db, actor, tenant_id, request_id):
    tenant, *_ = await context(db, actor, tenant_id, lock=True)
    request = await scoped(db, m.ActionRequest, tenant_id, request_id)
    if request.requester_user_id != actor.id:
        raise PermissionDeniedError("Only the original requesting identity can dispatch this action.")
    _, resource, evaluation = await authorize(db, actor, tenant_id, request)
    if evaluation.effect == "deny":
        await event(db, tenant_id, actor, "tool_denied", outcome="failure", target=request.id, details={"reason": evaluation.reason})
        await db.commit()
        raise PermissionDeniedError("Current policy denies this action.")
    if request.policy_version != tenant.policy_version or request.action_hash != fingerprint(request, resource):
        raise ConflictError("Authorization changed; propose a new action.")
    if request.status == "executed":
        # Even an idempotent replay must not disclose a result after source access is revoked.
        await source(db, actor, resource, request.action)
        return request
    if request.status != "ready" or request.expires_at <= now():
        raise ConflictError("The action is not ready or has expired.")
    if evaluation.required_approvals != request.required_approvals or await valid_approvals(db, request, resource) < request.required_approvals:
        raise PermissionDeniedError("The required independent approvals are missing or no longer authorized.")
    target = await source(db, actor, resource, request.action, lock=True)
    if request.agent_id:
        agent = await scoped(db, m.AgentIdentity, tenant_id, request.agent_id)
        if agent.window_started_at + timedelta(seconds=agent.window_seconds) <= now():
            agent.window_started_at, agent.calls_used = now(), 0
        if agent.calls_used >= agent.max_calls:
            await event(db, tenant_id, actor, "agent_budget_denied", outcome="failure", target=agent.id)
            await db.commit()
            raise PermissionDeniedError("This agent's execution budget is exhausted.")
        agent.calls_used += 1
        await db.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": str(agent.timeout_seconds * 1000)})
    if request.action == "document.metadata":
        request.result = {"document_id": str(target.id), "filename": target.filename, "visibility": target.visibility}
    elif request.action == "document.share":
        target.visibility = "shared"
        request.result = {"document_id": str(target.id), "visibility": "shared"}
    elif request.action == "agent.shutdown":
        target.enabled = False
        request.result = {"agent_id": str(target.id), "enabled": False}
    else:
        raise PermissionDeniedError("No registered adapter exists for this tool.")
    request.status, request.executed_at = "executed", now()
    await event(db, tenant_id, actor, "tool_executed", target=request.id, details={"action": request.action, "action_hash": request.action_hash, "agent_id": str(request.agent_id) if request.agent_id else None})
    await db.commit()
    return request
