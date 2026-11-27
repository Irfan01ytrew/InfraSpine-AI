"""Route external side effects (payments, WhatsApp sends, connector writes, recovery commands)
through `execute_once` so a retry or a duplicate webhook never dispatches twice.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.access import Principal
from app.platform.approvals import ActionSpec
from app.platform.execution import ExecutionResult, VerifiedResult, execute_once
from app.platform.sync import ensure_org_sync, module_scope_sync


async def org_scope_id(db: AsyncSession) -> uuid.UUID:
    return await db.run_sync(lambda s: ensure_org_sync(s.connection()))


async def module_scope_id(db: AsyncSession, module: str, tenant_id: uuid.UUID) -> uuid.UUID | None:
    return await db.run_sync(lambda s: module_scope_sync(s.connection(), module, tenant_id))


async def dispatch_once(
    db: AsyncSession, p: Principal, key: str, action: ActionSpec, *, duty: str,
    dispatch: Callable[[], Awaitable[str]],
    verify: Callable[[str], Awaitable[VerifiedResult]] | None = None,
    approval_id: uuid.UUID | None = None,
) -> ExecutionResult:
    async def _verify(ref: str) -> VerifiedResult:
        if verify is not None:
            return await verify(ref)
        return VerifiedResult(ok=bool(ref), details={"external_ref": ref})

    return await execute_once(db, p, key, action, duty=duty, dispatch=dispatch, verify=_verify,
                              approval_id=approval_id)
