"""Business continuity dashboard service layer (Step AJ, slice 2).

Wires the real sources into the slice-1 pure-function aggregator
(``app/continuity/dashboard_summary.py``) and returns one read-only
summary dict. No new tables or migrations: every section reads data
that already exists.

Sources, and how each is scope-filtered:

- Continuity (``app/continuity/service.py::dashboard``): nodes, links,
  assessments, plans, scenarios, runs and gaps are already filtered to
  the viewer's continuity scope before this module sees them. Gaps
  become risks (see ``GAP_SEVERITY``); pending plans/runs become
  approvals only for the roles that may decide them
  (``app/continuity/service.py::validate_decision`` requires owner or
  approver); runs become recovery items with verified/total steps taken
  from their tasks and the plan's step list, exactly like
  ``service.report`` counts them.
- Recovery incidents: recovery workspaces are a separate tenant
  namespace (``recovery_tenants``) with no mapping to continuity
  tenants, so the caller must supply a resolved recovery scope
  (``app/recovery/access.py::scope_for``). The API route tries to
  resolve one for the same tenant id and treats "no such recovery
  workspace" as "no incidents" — never an error, never a cross-
  workspace leak. When a recovery scope is supplied, incidents come
  from ``app/recovery/service.py::dashboard`` under that scope's own
  rules (non-owners only see their own incidents).
- Platform approvals inbox: the same query and per-row
  ``app/platform/access.py::authorize`` loop as
  ``GET /api/approvals`` — a row is listed only when the viewer is
  individually authorized for it on its own scope. Never trust the
  list query alone.
- Step AF alerts / Step AH predictions: the ``alerts.py`` /
  resilience modules live on sibling branches that are not merged
  into this branch yet, so they arrive as injected ``RiskInput``
  lists (``source="alert"`` / ``source="prediction"``). The wiring
  point is here and tested; the merge that lands AF/AH passes their
  output in. Nothing here fires, silences, or scores an alert.
- Health strip: the Step G roll-up (``app/continuity/health.py``)
  is likewise not in this branch yet, so ``health`` is passed
  through unchanged (``None`` until the Step G merge supplies it).

Read-only: this module never writes, dispatches, or decides
anything. No sealed payloads are decrypted or returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.continuity import service as continuity_service
from app.continuity.access import Scope
from app.continuity.dashboard_summary import (
    ApprovalInput,
    IncidentInput,
    RecoveryInput,
    RiskInput,
    build_dashboard_summary,
)
from app.continuity.models import Task
from app.db.models import User
from app.platform.access import authorize, principal_for
from app.platform.models import PlatformApprovalRequest
from app.recovery import service as recovery_service
from app.recovery.policy import TERMINAL as RECOVERY_TERMINAL

# Roles that may open the dashboard (plan §8 default: owner +
# department managers; planner/approver are the managing roles).
DASHBOARD_ROLES = ("owner", "approver", "planner")
# Roles that may decide a continuity plan/run (matches
# app/continuity/service.py::validate_decision).
DECIDING_ROLES = ("owner", "approver")

# Gaps come from intelligence.analyze over critical resources only, so
# every gap already concerns a critical resource. The severity ranks
# how directly the gap blocks recovery: no eligible backup means
# nobody can take over; a missing fallback or BIA leaves recovery
# unplanned; a single owner or missing documentation slows it down.
GAP_SEVERITY = {
    "missing_eligible_backup": "critical",
    "missing_bia": "high",
    "missing_alternative": "high",
    "single_owner": "medium",
    "missing_knowledge": "medium",
}


def _parse_dt(value):
    """Parse an ISO-8601 string from a ``public()`` dict (or pass a datetime through)."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _covered_resources(plans, now: datetime) -> set:
    """Resources covered by an approved, current plan.

    Same rule as ``app/continuity/service.py::dashboard``: the plan
    must be approved and its review due date still in the future.
    """
    covered = set()
    for plan in plans:
        if plan.get("status") != "approved":
            continue
        review_due = _parse_dt(plan.get("review_due_at"))
        if review_due is None or review_due <= now:
            continue
        for step in (plan.get("data") or {}).get("steps", []):
            if step.get("resource_id"):
                covered.add(str(step["resource_id"]))
    return covered


def _gap_risks(base: dict, covered: set) -> list:
    nodes = {str(n["id"]): n.get("name", str(n["id"])) for n in base.get("nodes", [])}
    risks = []
    for gap in base.get("gaps", []):
        node_id = str(gap.get("node_id", ""))
        risks.append(
            RiskInput(
                id=f"gap:{gap.get('code', 'unknown')}:{node_id}",
                title=nodes.get(node_id, node_id),
                severity=GAP_SEVERITY.get(gap.get("code"), "medium"),
                category=gap.get("code", "unknown"),
                source="gap",
                affected=(node_id,) if node_id else (),
                has_plan=node_id in covered,
            )
        )
    return risks


def _continuity_approvals(base: dict, scope: Scope) -> list:
    """Pending plan/run decisions, only for roles that may decide them."""
    if scope.role not in DECIDING_ROLES:
        return []
    plans = {str(p["id"]): p for p in base.get("plans", [])}
    approvals = []
    for plan in base.get("plans", []):
        if plan.get("status") != "pending":
            continue
        approvals.append(
            ApprovalInput(
                id=str(plan["id"]),
                action_type="continuity.plan_decision",
                subject=plan.get("title", ""),
                expires_at=_parse_dt(plan.get("approval_expires_at")),
                own_request=str(plan.get("requested_by") or "") == str(scope.user_id),
            )
        )
    for run in base.get("runs", []):
        if run.get("status") != "pending":
            continue
        plan = plans.get(str(run.get("plan_id") or ""))
        title = plan.get("title") if plan else None
        approvals.append(
            ApprovalInput(
                id=str(run["id"]),
                action_type="continuity.run_decision",
                subject=f"{run.get('mode', 'run')} run: {title}" if title else f"{run.get('mode', 'run')} run",
                expires_at=_parse_dt(run.get("approval_expires_at")),
                own_request=str(run.get("requested_by") or "") == str(scope.user_id),
            )
        )
    return approvals


async def _platform_approvals(db, scope: Scope, now: datetime) -> list:
    """Pending platform approvals, filtered exactly like the inbox."""
    user = await db.get(User, scope.user_id)
    if user is None:
        return []
    principal = principal_for(user)
    rows = (
        await db.scalars(
            select(PlatformApprovalRequest)
            .where(
                PlatformApprovalRequest.status == "pending",
                PlatformApprovalRequest.expires_at > now,
            )
            .order_by(PlatformApprovalRequest.created_at)
            .limit(500)
        )
    ).all()
    approvals = []
    for row in rows:  # authorise each on its own scope; never trust the list query alone
        decision = await authorize(
            db, principal, row.required_permission, row.scope_id, amount=row.amount
        )
        if decision.allowed:
            approvals.append(
                ApprovalInput(
                    id=str(row.id),
                    action_type=row.action_type,
                    subject=row.subject or "",
                    expires_at=row.expires_at,
                    own_request=row.requested_by == user.id,
                )
            )
    return approvals


async def _recovery_inputs(db, scope: Scope, runs: list, plans: list, now: datetime) -> list:
    """Continuity runs as recovery items, with verified/total steps."""
    plans_by_id = {str(p["id"]): p for p in plans}
    run_ids = [UUID(str(r["id"])) for r in runs]
    verified: dict[str, int] = {}
    if run_ids:
        rows = await db.scalars(
            select(Task).where(
                Task.tenant_id == scope.tenant_id, Task.run_id.in_(run_ids)
            )
        )
        for task in rows:
            if task.status == "verified":
                key = str(task.run_id)
                verified[key] = verified.get(key, 0) + 1
    recoveries = []
    for run in runs:
        plan = plans_by_id.get(str(run.get("plan_id") or ""))
        steps = (plan.get("data") or {}).get("steps", []) if plan else []
        total_steps = len(steps)
        verified_steps = min(verified.get(str(run["id"]), 0), total_steps)
        rto_minutes = None
        if plan:
            objectives = (plan.get("data") or {}).get("objective_snapshots") or {}
            rtos = [
                o.get("rto_minutes")
                for o in objectives.values()
                if isinstance(o, dict) and o.get("rto_minutes") is not None
            ]
            if rtos:
                rto_minutes = min(rtos)
        disruption_at = _parse_dt(run.get("disruption_at"))
        elapsed = None
        if disruption_at is not None:
            elapsed = max(0.0, (now - disruption_at).total_seconds() / 60)
        title = plan.get("title") if plan else None
        recoveries.append(
            RecoveryInput(
                id=str(run["id"]),
                title=title or f"{run.get('mode', 'run')} run",
                status=run.get("status", "pending"),
                verified_steps=verified_steps,
                total_steps=total_steps,
                rto_minutes=rto_minutes,
                elapsed_minutes=elapsed,
            )
        )
    return recoveries


async def continuity_dashboard_summary(
    db,
    scope: Scope,
    *,
    recovery_scope=None,
    alerts=(),
    predictions=(),
    health=None,
    now: datetime | None = None,
) -> dict:
    """Build the read-only continuity dashboard summary for ``scope``.

    ``recovery_scope`` is a resolved ``app.recovery`` scope (or
    ``None`` when the viewer has no recovery workspace for this
    tenant); ``alerts`` / ``predictions`` are ``RiskInput`` lists from
    Step AF / Step AH; ``health`` is the Step G roll-up dict (or
    ``None``). ``now`` defaults to the current UTC time.
    """
    now = now or datetime.now(UTC)
    base = await continuity_service.dashboard(db, scope)
    plans = base.get("plans", [])
    runs = base.get("runs", [])
    covered = _covered_resources(plans, now)

    risks = _gap_risks(base, covered)
    risks.extend(alerts)
    risks.extend(predictions)

    incidents = []
    if recovery_scope is not None:
        rec = await recovery_service.dashboard(db, recovery_scope)
        for row in rec.get("incidents", []):
            sla_due = _parse_dt(row.get("sla_due"))
            incidents.append(
                IncidentInput(
                    id=str(row["id"]),
                    title=row.get("title", ""),
                    severity=row.get("severity", "medium"),
                    status=row.get("status", "detected"),
                    overdue=bool(
                        row.get("status") not in RECOVERY_TERMINAL
                        and sla_due is not None
                        and sla_due < now
                    ),
                )
            )

    approvals = _continuity_approvals(base, scope)
    approvals.extend(await _platform_approvals(db, scope, now))
    recoveries = await _recovery_inputs(db, scope, runs, plans, now)

    return build_dashboard_summary(
        risks=risks,
        incidents=incidents,
        approvals=approvals,
        recoveries=recoveries,
        health=health,
        now=now,
    )
