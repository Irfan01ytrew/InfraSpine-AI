"""Step N slice 3 — cost intelligence API (read-only).

``GET /api/cost-intelligence/scopes/{scope_id}/summary`` (+ per-agent and
per-department variants). Every read goes through ``app/platform``:
``require(db, p, "cost.read", scope_id)`` — 404 for non-members and for
scopes with no cost view (unknown scope, ``module_space``, workspace with
no department ancestor), 403 for members without the permission. The
permission is ``cost.read`` (``app.core.permission_catalog``); the owner
holds it via a finance role granted on the org scope — the scope chain
then covers every descendant scope — and department managers hold it on
their own department only (plan §7.3).

The endpoints are read-only: they call ``scope_ledger()`` (slice 2),
which only SELECTs, and never commit. A GET writes nothing — proven by
``tests/test_cost_intelligence_api.py::test_get_writes_nothing``.

Provenance: the ``sources`` map in every response names which ledger
source each figure came from. While AK (Step AK) and T (Step T) are
unmerged, their entries read ``unavailable: ...`` and the corresponding
figures report ``recorded_unavailable`` / ``routing=None`` — never a
silent zero.

Slice 4 adds ``GET /api/cost-intelligence/scopes/{scope_id}/inventory``:
the read-only control-plane inventory (identities/memberships, policies,
agents, workflow templates, knowledge ACLs, integration connections),
gated by ``control.read`` (``app.core.permission_catalog``) instead of
``cost.read``. Same 404/403 rules; a ``module_space`` has no control view.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.errors import InvalidInputError, NotFoundError
from app.core.permission_catalog import PERMISSION_CONTROL_READ, PERMISSION_COST_READ
from app.cost_intelligence.inventory import control_inventory
from app.cost_intelligence.ledger import department_scopes_for, scope_ledger
from app.platform import access as platform_access

router = APIRouter(prefix="/api/cost-intelligence", tags=["cost-intelligence"])


async def _gate(db, user, scope_id: uuid.UUID):
    """Platform gate for a cost read; returns the scope ref.

    ``require()`` raises 404 for a missing scope or a non-member and 403
    for a member without ``cost.read``. A ``module_space`` has no cost
    view at all (slice 2: no department attribution exists for run data
    there), so it is 404 even for a permitted member.
    """
    principal = platform_access.principal_for(user)
    scope = await platform_access.require(db, principal, PERMISSION_COST_READ, scope_id)
    if scope.kind == "module_space":
        raise NotFoundError("Not found.")
    return scope


def _since(value: datetime | None) -> datetime | None:
    """A naive ``since`` is read as UTC; aware values pass through."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _ledger(db, scope_id: uuid.UUID, since: datetime | None):
    """One scope's ledger; scope-attribution problems are 404, a ledger
    too large to report (over ``MAX_RECORDS`` runs) is 422, never a
    silent truncation."""
    try:
        return await scope_ledger(db, scope_id=scope_id, since=since)
    except ValueError as exc:
        if "refusing to truncate" in str(exc):
            raise InvalidInputError(
                "Too many runs in this scope to report in one response."
            ) from exc
        raise NotFoundError("Not found.") from exc


@router.get("/scopes/{scope_id}/summary")
async def cost_scope_summary(
    scope_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    since: datetime | None = Query(default=None),
) -> dict:
    """Full cost ledger for one scope: figures, per-agent figures,
    estimated-vs-recorded reconciliation, routing comparison, provenance
    in ``sources``, and the ledger fingerprint."""
    await _gate(db, user, scope_id)
    ledger = await _ledger(db, scope_id, _since(since))
    return ledger.as_dict()


@router.get("/scopes/{scope_id}/agents")
async def cost_scope_agents(
    scope_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    since: datetime | None = Query(default=None),
) -> dict:
    """Per-agent cost figures + reconciliation for one scope, one entry
    per agent with runs (or recorded spend) in the scope."""
    await _gate(db, user, scope_id)
    ledger = await _ledger(db, scope_id, _since(since))
    agents = [
        {
            "agent_id": agent_id,
            "figures": figures.as_dict(),
            "reconciliation": (
                ledger.reconciliation[agent_id].as_dict()
                if agent_id in ledger.reconciliation
                else None
            ),
        }
        for agent_id, figures in sorted(ledger.by_agent.items())
    ]
    return {
        "scope_id": str(scope_id),
        "agents": agents,
        "fingerprint": ledger.fingerprint,
        "sources": dict(ledger.sources),
    }


@router.get("/scopes/{scope_id}/agents/{agent_id}")
async def cost_scope_agent(
    scope_id: uuid.UUID,
    agent_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    since: datetime | None = Query(default=None),
) -> dict:
    """One agent's cost figures + reconciliation inside one scope.
    404 when the agent has no runs (and no recorded spend) in the scope."""
    await _gate(db, user, scope_id)
    ledger = await _ledger(db, scope_id, _since(since))
    key = str(agent_id)
    figures = ledger.by_agent.get(key)
    reconciliation = ledger.reconciliation.get(key)
    if figures is None and reconciliation is None:
        raise NotFoundError("Not found.")
    return {
        "scope_id": str(scope_id),
        "agent_id": key,
        "figures": figures.as_dict() if figures is not None else None,
        "reconciliation": reconciliation.as_dict() if reconciliation is not None else None,
        "sources": dict(ledger.sources),
    }


async def _gate_control(db, user, scope_id: uuid.UUID):
    """Platform gate for a control-plane inventory read; returns the scope ref.

    ``require()`` raises 404 for a missing scope or a non-member and 403
    for a member without ``control.read``. A ``module_space`` has no
    control view at all, so it is 404 even for a permitted member.
    """
    principal = platform_access.principal_for(user)
    scope = await platform_access.require(db, principal, PERMISSION_CONTROL_READ, scope_id)
    if scope.kind == "module_space":
        raise NotFoundError("Not found.")
    return scope


@router.get("/scopes/{scope_id}/inventory")
async def cost_scope_inventory(
    scope_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """The read-only control-plane inventory for one scope: identities /
    memberships, roles / policies, unified agent views, workflow
    templates, knowledge ACLs, and integration connections — with a
    provenance map and a fingerprint. Never writes anything."""
    await _gate_control(db, user, scope_id)
    try:
        inventory = await control_inventory(db, scope_id)
    except ValueError as exc:
        raise NotFoundError("Not found.") from exc
    return inventory.as_dict()


@router.get("/scopes/{scope_id}/departments")
async def cost_scope_departments(
    scope_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    since: datetime | None = Query(default=None),
) -> dict:
    """Per-department cost figures rolling up into one scope: every
    department under an org scope, the scope itself for a department,
    the nearest department ancestor for a workspace. Each department's
    figures come from its own ``scope_ledger()`` call, so the one-run-
    one-department attribution rule is never re-implemented here."""
    await _gate(db, user, scope_id)
    try:
        departments = await department_scopes_for(db, scope_id)
    except ValueError as exc:
        raise NotFoundError("Not found.") from exc
    out = []
    for department in departments:
        ledger = await _ledger(db, department.id, _since(since))
        out.append(
            {
                "scope_id": str(department.id),
                "name": department.name,
                "figures": ledger.figures.as_dict(),
                "fingerprint": ledger.fingerprint,
            }
        )
    return {"scope_id": str(scope_id), "departments": out}
