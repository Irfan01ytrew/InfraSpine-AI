"""Module adoption of the platform core (step D): one gate for every module, channel and worker.

Each module keeps computing its legacy decision (its member row and role) and asks `gate()` for the platform
decision on the same scope. Modes (env PLATFORM_ACCESS_MODE):

* enforce (default)  access needs BOTH decisions: the platform can only take access away (revoked membership,
                     revoked/stale principal, expired time-limited grant, inactive scope), never add it.
* shadow             the legacy decision is used and differences are only logged — the documented rollback
                     switch if a platform defect ever blocks legitimate work.

Every difference is logged on logger "app.platform.shadow" and kept in a small in-memory ring (`recent_diffs()`)
so tests and the admin health view can show it. Missing scopes/memberships are created on first use from the
legacy rows (the fallback when backfill has not run), see app.platform.sync.
"""

from __future__ import annotations

import collections
import logging
import os
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User
from app.platform import sync
from app.platform.access import Principal, authorize, principal_for

logger = logging.getLogger("app.platform.shadow")
_DIFFS: collections.deque = collections.deque(maxlen=500)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    legacy: bool
    platform: bool
    reason: str
    scope_id: uuid.UUID | None


def mode() -> str:
    value = os.environ.get("PLATFORM_ACCESS_MODE", "enforce").strip().lower()
    return value if value in {"enforce", "shadow"} else "enforce"


def recent_diffs() -> list[dict]:
    return list(_DIFFS)


def clear_diffs() -> None:
    _DIFFS.clear()


def _record(kind: str, key: str, user_id, permission: str, legacy: bool, platform: bool, reason: str) -> None:
    if legacy == platform:
        return
    entry = {"kind": kind, "key": key, "user_id": str(user_id), "permission": permission, "legacy": legacy,
             "platform": platform, "reason": reason, "mode": mode()}
    _DIFFS.append(entry)
    logger.info("platform access diff %s", entry)


def _final(legacy: bool, platform: bool) -> bool:
    return legacy if mode() == "shadow" else (legacy and platform)


async def as_principal(db: AsyncSession, user: User | Principal, *, channel: str = "web") -> Principal:
    """Principal for a user object; reads auth_version from the database if the object no longer has it loaded."""
    if isinstance(user, Principal):
        return user
    from sqlalchemy import inspect as sa_inspect, select
    state = sa_inspect(user)
    if state.unloaded & {"id", "email", "auth_version"}:
        identity = state.identity[0] if state.identity else user.id
        row = (await db.execute(select(User.id, User.email, User.auth_version).where(User.id == identity))).first()
        if row is None:
            raise NotFoundError("Not found.")
        return Principal(row.id, row.email, "user", channel, row.auth_version, uuid.uuid4())
    return principal_for(user, channel=channel)


async def _decide(db: AsyncSession, p: Principal, permission: str, scope_id: uuid.UUID | None) -> tuple[bool, str]:
    if scope_id is None:
        return False, "scope_missing"
    decision = await authorize(db, p, permission, scope_id)
    return decision.allowed, decision.reason


async def module_gate(db: AsyncSession, module: str, tenant_id: uuid.UUID, user: User | Principal, *,
                      legacy_allowed: bool, permission: str | None = None) -> GateResult:
    """Platform decision for a module tenant (company_graph, governance, continuity, process_intelligence, recovery)."""
    p = await as_principal(db, user)
    scope_id, _ = await db.run_sync(lambda s: sync.reconcile_module_member(s.connection(), module, tenant_id, p.user_id))
    permission = permission or f"{module}.access"
    platform, reason = await _decide(db, p, permission, scope_id)
    _record("module", f"{module}:{tenant_id}", p.user_id, permission, legacy_allowed, platform, reason)
    return GateResult(_final(legacy_allowed, platform), legacy_allowed, platform, reason, scope_id)


async def group_gate(db: AsyncSession, group_id: uuid.UUID, user: User | Principal, *, legacy_allowed: bool,
                     permission: str = "workflow.access") -> GateResult:
    """Platform decision for a department/workspace (access group) scope, e.g. workflow authority."""
    p = await as_principal(db, user)

    def reconcile(s):
        conn = s.connection()
        sync.reconcile_user(conn, p.user_id)
        return sync.reconcile_group_member(conn, group_id, p.user_id)[0]
    scope_id = await db.run_sync(reconcile)
    platform, reason = await _decide(db, p, permission, scope_id)
    _record("group", f"group:{group_id}", p.user_id, permission, legacy_allowed, platform, reason)
    return GateResult(_final(legacy_allowed, platform), legacy_allowed, platform, reason, scope_id)


async def org_gate(db: AsyncSession, user: User | Principal, permission: str, *, legacy_allowed: bool = True) -> GateResult:
    """Organisation-wide permission (agent.run, documents.read, channel.reply, qa.manage …)."""
    p = await as_principal(db, user)
    scope_id, _ = await db.run_sync(lambda s: sync.reconcile_user(s.connection(), p.user_id))
    platform, reason = await _decide(db, p, permission, scope_id)
    _record("org", "org", p.user_id, permission, legacy_allowed, platform, reason)
    return GateResult(_final(legacy_allowed, platform), legacy_allowed, platform, reason, scope_id)


def raise_unless(result: GateResult, *, not_found: str = "Not found.", denied: str | None = None) -> None:
    """Module-style errors: no membership -> 404 (resource not revealed), otherwise 403."""
    if result.allowed:
        return
    if denied and result.legacy and result.reason == "permission_missing":
        raise PermissionDeniedError(denied)
    raise NotFoundError(not_found)
