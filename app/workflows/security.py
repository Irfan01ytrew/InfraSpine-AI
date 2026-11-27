"""Reuse application identity, department authorization and financial authority."""

import base64
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

from app.core.crypto import EncryptedValue
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User
from app.db.approval_models import ApprovalDelegation
from app.services.approval_authority import effective_limit
from app.workflows.definition import lookup
from app.platform.modules import group_gate
from app.platform.timezone import org_today


def seal(cipher, scope_id, kind, row_id, value):
    encrypted = cipher.encrypt(json.dumps(value, ensure_ascii=False, allow_nan=False),
                               associated_data=f'wf05:{scope_id}:{kind}:{row_id}'.encode())
    return {'ciphertext': base64.b64encode(encrypted.ciphertext).decode(),
            'nonce': base64.b64encode(encrypted.nonce).decode(), 'key_id': encrypted.key_id}


def unseal(cipher, scope_id, kind, row_id, value):
    encrypted = EncryptedValue(base64.b64decode(value['ciphertext']), base64.b64decode(value['nonce']), value['key_id'])
    return json.loads(cipher.decrypt(encrypted, associated_data=f'wf05:{scope_id}:{kind}:{row_id}'.encode()))


async def is_manager(db, actor, scope_id):
    return actor.is_active and (actor.role == 'admin' or actor.role == 'manager'
        and await db.get(DepartmentManager, (scope_id, actor.id)) is not None)


async def require_scope(db, actor, scope_id, *, manage=False):
    group = await db.get(AccessGroup, scope_id)
    if group is None or group.kind != 'department' or not actor.is_active:
        raise NotFoundError('Workflow scope not found.')
    manager = await is_manager(db, actor, scope_id)
    legacy = manager or (not manage and await db.get(AccessGroupMember, (scope_id, actor.id)) is not None)
    gate = await group_gate(db, scope_id, actor, legacy_allowed=legacy,
                            permission='workflow.manage' if manage or manager else 'workflow.access')
    if gate.allowed:
        return group
    raise PermissionDeniedError('Current department membership and workflow authority are required.')


async def require_member(db, scope_id, user_id):
    person = await db.get(User, user_id)
    if person is None:
        raise InvalidInputError('Choose an active member of this workflow scope.')
    await require_scope(db, person, scope_id)
    return person


async def require_owner(db, actor, workflow):
    await require_scope(db, actor, workflow.scope_id)
    if workflow.owner_id != actor.id and not await is_manager(db, actor, workflow.scope_id):
        raise PermissionDeniedError('Only the owner or an authorized department manager can change this workflow.')


async def decision_authority(db, actor, scope_id, requester_id, node, context, on_behalf_of=None):
    await require_scope(db, actor, scope_id)
    authority = actor
    if on_behalf_of and on_behalf_of != actor.id:
        today = await org_today(db)
        delegation = await db.scalar(select(ApprovalDelegation).where(
            ApprovalDelegation.delegator_user_id == on_behalf_of,
            ApprovalDelegation.delegate_user_id == actor.id,
            ApprovalDelegation.is_active.is_(True), ApprovalDelegation.starts_on <= today,
            ApprovalDelegation.ends_on >= today,
        ))
        if delegation is None:
            raise PermissionDeniedError('No current authorized delegation exists.')
        authority = await require_member(db, scope_id, on_behalf_of)
    if actor.id == requester_id or authority.id == requester_id:
        raise PermissionDeniedError('Separation of duties forbids approving your own request.')
    if node.config.approver_ids and authority.id not in node.config.approver_ids:
        raise PermissionDeniedError('This action requires a named approval authority.')
    if node.risk in {'high', 'critical'}:
        await require_scope(db, authority, scope_id, manage=True)
        # Delegation cannot elevate an unprivileged delegate into a manager.
        await require_scope(db, actor, scope_id, manage=True)
    if node.config.financial_category:
        try:
            amount = Decimal(str(lookup(node.config.amount_variable, context)))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation()
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidInputError('A positive, finite financial amount is required.') from None
        for person in {actor.id: actor, authority.id: authority}.values():
            limit, _ = await effective_limit(db, person, node.config.financial_category)
            if limit is None or limit.currency != node.config.currency or limit.max_amount < amount:
                raise PermissionDeniedError('Current financial approval authority is insufficient.')
    return authority
