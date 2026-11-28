"""Shared platform core: identity, scopes, permissions, approvals, exactly-once execution, events and org time.

Every module and channel calls these instead of keeping its own tenant/member and approval logic (adoption is
step D). See docs/platform-core.md.
"""

from app.platform.access import (
    ADMIN_BRIDGE_PERMISSIONS, APPROVAL_REQUIRED, SEPARATED_DUTIES, Decision, Principal, ScopeFilter, ScopeRef,
    authorize, create_scope, effective_permissions, ensure_org_scope, ensure_role, grant_membership, require,
    resolve_principal, revoke_user_access, visible_scopes,
)
from app.platform.approvals import ActionSpec, ApprovalGrant, consume_approval, decide_approval, request_approval
from app.platform.events import audit, drain_outbox, publish
from app.platform.execution import ExecutionResult, VerifiedResult, execute_once
from app.platform.timezone import org_today, org_today_sql, org_tz, set_org_time_zone

__all__ = [
    "ADMIN_BRIDGE_PERMISSIONS", "APPROVAL_REQUIRED", "SEPARATED_DUTIES", "ActionSpec", "ApprovalGrant", "Decision",
    "ExecutionResult", "Principal", "ScopeFilter", "ScopeRef", "VerifiedResult", "audit", "authorize",
    "consume_approval", "create_scope", "decide_approval", "drain_outbox", "effective_permissions",
    "ensure_org_scope", "ensure_role", "execute_once", "grant_membership", "org_today", "org_today_sql", "org_tz",
    "publish", "request_approval", "require", "resolve_principal", "revoke_user_access", "set_org_time_zone",
    "visible_scopes",
]
