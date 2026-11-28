"""Cost ledger service (Step N, slice 2).

Read-only aggregation over the three ledger sources the plan names
(``docs/step-n-cost-intelligence-plan.md`` §5 slice 2):

1. **Studio runs** — ``AgentStudioRun`` rows. This is the only source that
   exists on this base, so it is the only source read from the database here.
   ``estimated_cost_usd`` on a run is an *estimate* recorded at run time,
   never a money fact.
2. **AK spend ledger** — ``AgentSpendLedger`` (Step AK, branch
   ``agent/agent-controls``). This is the **money truth** for enforced spend:
   idempotent spend recorded through ``execute_once``. It does **not** exist
   on this base yet, so :func:`ak_recorded_spend` raises
   :class:`LedgerSourceUnavailable` naming the integration point until AK
   merges. This module never re-implements spend logic.
3. **T routing records** — pre-run cost estimates from the Step T model
   router (branch ``agent/model-router``). Also absent on this base; see
   :func:`routing_estimates`.

Provenance — which figure each field comes from (plan §8):

- ``ScopeLedger.figures`` / ``by_agent`` — sums of recorded Studio runs,
  computed by the slice-1 pure engine (``app.cost_intelligence.figures``).
  Money is ``Decimal`` only; a run with no configured rate counts in
  ``cost_unknown_count`` and is never counted as zero cost.
- ``Reconciliation.estimated_total`` — sum of ``estimated_cost_usd`` over
  the scope's Studio runs (estimate).
- ``Reconciliation.recorded_total`` — sum of AK ``AgentSpendLedger`` rows
  (money truth). ``None`` while AK is unmerged; an agent with no ledger
  entries is ``no_recorded_spend``, never zero.
- ``RoutingComparison`` — Studio estimate vs T pre-run estimate for the
  same run id. A mismatch means the estimate changed between routing and
  the run; it never changes a figure, it is only reported.
- Human cost / ROI stay ``unknown`` in this slice: no human-time records
  exist on this base, and slice 1 already proves ``unknown`` is carried
  through every roll-up. The caller may not inject human minutes/rates
  here — that is slice 3's API concern, if the plan ever adds it.

Scope attribution — Studio runs are owner-scoped (``AgentStudioRun`` has
no scope column), so a run's scope is derived from its owner, following
the rule the codebase already uses for the same question:
``app.agent_studio.unified.scope_for_owner`` — "the first (by name)
department they may use workflows in". Translated into platform terms:

- A run belongs to exactly one department: its owner's first (by name)
  active department membership. No leak (one department never sees
  another's spend) and no double count (a run is counted once per scope).
- The org scope sees every run (org-wide roll-up is the whole company).
- A workspace under a department sees the runs of its own active members
  whose first department is that parent department.
- ``module_space`` scopes have no department attribution from run data
  and raise ``ValueError`` — the API layer (slice 3) must not offer a
  cost view there.
- An owner with no department at all has runs no department can see;
  they still count in the org roll-up. Membership is the active rule
  from ``app.platform.access`` (not revoked, within valid_from/valid_to).

Text is untrusted data: agent names, descriptions, task text and
run notes are never read — only numeric / status fields feed the math.

This service writes nothing. It never approves, pays, or changes a
permission; it only reports. No migration in this slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cost_intelligence.figures import (
    MAX_RECORDS,
    CostFigures,
    RunRecord,
    figures_fingerprint,
    rollup,
    summarize_by_agent,
    validate_records,
)
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.platform.models import PlatformMembership, PlatformScope

__all__ = [
    "LedgerSourceUnavailable",
    "RecordedSpend",
    "RoutingEstimate",
    "RoutingComparison",
    "Reconciliation",
    "ScopeLedger",
    "MAX_RECORDS",
    "scope_member_user_ids",
    "department_for_owner",
    "attributed_owner_ids",
    "department_scopes_for",
    "scope_agent_ids",
    "studio_run_records",
    "ak_recorded_spend",
    "routing_estimates",
    "reconcile",
    "compare_routing_estimates",
    "scope_ledger",
]

_MONEY_Q = Decimal("0.0001")
_PCT_Q = Decimal("0.01")
_MAX_MISMATCH_IDS = 100

# Reconciliation statuses. "matched" means estimated == recorded to 4dp.
RECONCILIATION_STATUSES = frozenset(
    {
        "matched",
        "estimated_higher",
        "recorded_higher",
        "recorded_unavailable",
        "no_recorded_spend",
    }
)


class LedgerSourceUnavailable(RuntimeError):
    """A ledger source module is not on this branch yet.

    Raised (never swallowed) by the direct source readers so a caller can
    tell "source not merged" apart from "source says zero". The
    orchestrator :func:`scope_ledger` catches it and degrades gracefully,
    recording the reason in ``ScopeLedger.sources``.
    """


def _money(value: Any, field_name: str) -> Decimal:
    """Exact money only: Decimal or int, never bool/float/str, never negative."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{field_name} must be a Decimal (never float or str)")
    result = Decimal(value)
    if result < 0:
        raise ValueError(f"{field_name} must not be negative")
    return result


def _uuid(value: Any, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ValueError(f"{field_name} must be a UUID")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_Q)


@dataclass(frozen=True)
class RecordedSpend:
    """One agent's recorded spend from the AK spend ledger (money truth)."""

    agent_id: str
    total_recorded: Decimal
    entries: int
    source: str = "ak_spend_ledger"

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "total_recorded": str(self.total_recorded),
            "entries": self.entries,
            "source": self.source,
        }


@dataclass(frozen=True)
class RoutingEstimate:
    """One run's pre-run cost estimate from the Step T model router."""

    run_id: str
    agent_id: str
    estimated_cost_usd: Decimal | None
    model: str | None = None
    source: str = "model_router"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "estimated_cost_usd": None
            if self.estimated_cost_usd is None
            else str(self.estimated_cost_usd),
            "model": self.model,
            "source": self.source,
        }


@dataclass(frozen=True)
class RoutingComparison:
    """Studio estimate vs T pre-run estimate for the same run ids.

    A mismatch is reported, never "fixed" by rewriting a figure — the
    estimate changed between routing and the run, and the ledger keeps
    both values.
    """

    runs_compared: int
    matched: int
    mismatched: int
    routing_estimated_total: Decimal
    studio_estimated_total: Decimal
    mismatched_run_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs_compared": self.runs_compared,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "routing_estimated_total": str(self.routing_estimated_total),
            "studio_estimated_total": str(self.studio_estimated_total),
            "mismatched_run_ids": list(self.mismatched_run_ids),
        }


@dataclass(frozen=True)
class Reconciliation:
    """Estimated (Studio) vs recorded (AK ledger) cost for one agent.

    ``recorded_total`` / ``variance`` / ``variance_pct`` are ``None`` when
    the AK ledger is unavailable or the agent has no ledger entries —
    absence of a record is never reported as zero spend.
    """

    agent_id: str
    estimated_total: Decimal
    recorded_total: Decimal | None
    variance: Decimal | None  # estimated - recorded, 4dp
    variance_pct: Decimal | None  # variance / recorded * 100, 2dp
    status: str
    estimated_runs: int
    recorded_entries: int
    unpriced_runs: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "estimated_total": str(self.estimated_total),
            "recorded_total": None if self.recorded_total is None else str(self.recorded_total),
            "variance": None if self.variance is None else str(self.variance),
            "variance_pct": None if self.variance_pct is None else str(self.variance_pct),
            "status": self.status,
            "estimated_runs": self.estimated_runs,
            "recorded_entries": self.recorded_entries,
            "unpriced_runs": self.unpriced_runs,
        }


@dataclass(frozen=True)
class ScopeLedger:
    """Everything slice 2 knows about one scope's cost, with provenance."""

    scope_id: str
    figures: CostFigures
    by_agent: dict[str, CostFigures] = field(default_factory=dict)
    reconciliation: dict[str, Reconciliation] = field(default_factory=dict)
    routing: RoutingComparison | None = None
    recorded_total: Decimal | None = None
    sources: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "figures": self.figures.as_dict(),
            "by_agent": {k: v.as_dict() for k, v in self.by_agent.items()},
            "reconciliation": {k: v.as_dict() for k, v in self.reconciliation.items()},
            "routing": None if self.routing is None else self.routing.as_dict(),
            "recorded_total": None if self.recorded_total is None else str(self.recorded_total),
            "sources": dict(self.sources),
            "fingerprint": self.fingerprint,
        }


def _active_clause(membership: Any, now: datetime) -> Any:
    """Active-membership rule, same as ``app.platform.access._active_membership``."""
    return and_(
        membership.revoked_at.is_(None),
        or_(membership.valid_from.is_(None), membership.valid_from <= now),
        or_(membership.valid_to.is_(None), membership.valid_to > now),
    )


async def scope_member_user_ids(db: AsyncSession, scope_id: UUID) -> frozenset[UUID]:
    """Active member user ids of one scope (the scope itself, not descendants)."""
    scope_id = _uuid(scope_id, "scope_id")
    now = datetime.now(UTC)
    rows = await db.scalars(
        select(PlatformMembership.user_id).where(
            PlatformMembership.scope_id == scope_id,
            _active_clause(PlatformMembership, now),
        )
    )
    return frozenset(rows)


async def department_for_owner(db: AsyncSession, owner_id: UUID) -> UUID | None:
    """The owner's department for their Studio runs.

    First (by name, then id) active department membership — the platform
    translation of ``app.agent_studio.unified.scope_for_owner``. ``None``
    when the owner holds no department membership.
    """
    owner_id = _uuid(owner_id, "owner_id")
    now = datetime.now(UTC)
    return await db.scalar(
        select(PlatformScope.id)
        .join(PlatformMembership, PlatformMembership.scope_id == PlatformScope.id)
        .where(
            PlatformMembership.user_id == owner_id,
            PlatformScope.kind == "department",
            PlatformScope.active.is_(True),
            _active_clause(PlatformMembership, now),
        )
        .order_by(PlatformScope.name, PlatformScope.id)
        .limit(1)
    )


async def _nearest_department_ancestor(db: AsyncSession, scope: PlatformScope) -> UUID | None:
    """Nearest department ancestor of a scope (itself when it is one)."""
    current: PlatformScope | None = scope
    seen: set[UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.kind == "department":
            return current.id
        if current.parent_id is None:
            return None
        current = await db.get(PlatformScope, current.parent_id)
    return None


async def attributed_owner_ids(db: AsyncSession, scope_id: UUID) -> frozenset[UUID] | None:
    """Owner ids whose Studio runs belong to a scope; ``None`` = all owners.

    - org scope → ``None`` (the org roll-up is the whole company).
    - department → active members whose first department is this scope.
    - workspace → active members whose first department is the nearest
      department ancestor (raises ``ValueError`` when there is none).
    - ``module_space`` → ``ValueError`` (no department attribution exists
      for run data there).
    """
    scope_id = _uuid(scope_id, "scope_id")
    scope = await db.get(PlatformScope, scope_id)
    if scope is None or not scope.active:
        raise ValueError(f"unknown or inactive scope: {scope_id}")
    if scope.kind == "org":
        return None
    department_id = await _nearest_department_ancestor(db, scope)
    if department_id is None:
        raise ValueError(
            f"scope {scope_id} ({scope.kind}) has no department ancestor; "
            "cost ledgers are computed per department, workspace, or org"
        )
    members = await scope_member_user_ids(db, scope_id)
    owners: set[UUID] = set()
    for owner_id in members:
        if await department_for_owner(db, owner_id) == department_id:
            owners.add(owner_id)
    return frozenset(owners)


async def department_scopes_for(db: AsyncSession, scope_id: UUID) -> list[PlatformScope]:
    """Department scopes whose cost ledgers roll up into ``scope_id``.

    - org scope → every active department (ordered by name, then id).
    - department → itself.
    - workspace → its nearest department ancestor.
    - ``module_space`` → ``ValueError`` (no cost view there — the API
      layer must not offer one).

    Raises ``ValueError`` for an unknown or inactive scope, mirroring
    :func:`attributed_owner_ids`. The slice-3 API uses this for its
    per-department endpoint; each department's figures still come from
    :func:`scope_ledger`, so the attribution rule is never re-implemented.
    """
    scope_id = _uuid(scope_id, "scope_id")
    scope = await db.get(PlatformScope, scope_id)
    if scope is None or not scope.active:
        raise ValueError(f"unknown or inactive scope: {scope_id}")
    if scope.kind == "org":
        rows = await db.scalars(
            select(PlatformScope)
            .where(PlatformScope.kind == "department", PlatformScope.active.is_(True))
            .order_by(PlatformScope.name, PlatformScope.id)
        )
        return list(rows)
    if scope.kind == "department":
        return [scope]
    if scope.kind == "module_space":
        raise ValueError(
            f"scope {scope_id} is a module_space; cost ledgers are computed "
            "per department, workspace, or org"
        )
    department_id = await _nearest_department_ancestor(db, scope)
    if department_id is None:
        raise ValueError(
            f"scope {scope_id} ({scope.kind}) has no department ancestor; "
            "cost ledgers are computed per department, workspace, or org"
        )
    department = await db.get(PlatformScope, department_id)
    return [department] if department is not None and department.active else []


async def scope_agent_ids(
    db: AsyncSession, *, member_user_ids: Iterable[UUID] | None
) -> frozenset[str]:
    """Agent ids owned by the given users (``None`` = all agents).

    Used so reconciliation also sees agents with recorded (AK) spend but
    no Studio runs in the window — they report ``recorded_higher`` with
    zero estimated cost, never silently dropped.
    """
    if member_user_ids is None:
        rows = await db.scalars(select(AgentStudioAgent.id))
        return frozenset(str(row) for row in rows)
    members = {_uuid(m, "member_user_ids entry") for m in member_user_ids}
    rows = await db.scalars(
        select(AgentStudioAgent.id).where(AgentStudioAgent.owner_id.in_(members))
    )
    return frozenset(str(row) for row in rows)


def _row_to_record(row: AgentStudioRun, scope_id: UUID) -> RunRecord:
    """Map one Studio run row to a slice-1 record.

    Only numeric / status fields are read. ``usage_by_model`` is not read
    (per-model re-pricing of past runs is out of scope — plan §4), and no
    text column (agent name, description, task text) is ever read.
    """
    return RunRecord(
        id=str(row.id),
        agent_id=str(row.agent_id),
        scope_id=str(scope_id),
        status=row.status,
        estimated_cost_usd=row.estimated_cost_usd,
        prompt_tokens=int(row.prompt_tokens or 0),
        completion_tokens=int(row.completion_tokens or 0),
        human_minutes=None,  # no human-time source exists on this base
        human_hourly_rate_usd=None,
        notes="",  # untrusted text is never read
    )


async def studio_run_records(
    db: AsyncSession,
    *,
    scope_id: UUID,
    owner_ids: Iterable[UUID] | None,
    since: datetime | None = None,
    limit: int = MAX_RECORDS,
) -> list[RunRecord]:
    """Studio runs owned by the given owners, as slice-1 records.

    ``owner_ids=None`` reads every owner's runs (org roll-up). Raises
    ``ValueError`` if more than ``limit`` rows match, instead of silently
    truncating (a truncated ledger would report wrong figures).
    """
    scope_id = _uuid(scope_id, "scope_id")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive int")
    stmt = select(AgentStudioRun)
    if owner_ids is not None:
        owners = {_uuid(o, "owner_ids entry") for o in owner_ids}
        stmt = stmt.where(AgentStudioRun.owner_id.in_(owners))
    if since is not None:
        if not isinstance(since, datetime):
            raise ValueError("since must be a datetime")
        stmt = stmt.where(AgentStudioRun.created_at >= since)
    stmt = stmt.order_by(AgentStudioRun.created_at, AgentStudioRun.id).limit(limit + 1)
    rows = list(await db.scalars(stmt))
    if len(rows) > limit:
        raise ValueError(f"more than {limit} runs in scope; refusing to truncate")
    return validate_records([_row_to_record(row, scope_id) for row in rows])


async def ak_recorded_spend(
    db: AsyncSession,
    *,
    agent_ids: Iterable[str],
    scope_id: UUID,
) -> dict[str, RecordedSpend]:
    """Recorded spend per agent from the AK spend ledger (money truth).

    Reader contract for the merge (to be implemented against the real AK
    store when ``agent/agent-controls`` lands): ``spend_by_agent(db, *,
    agent_ids, scope_id)`` returning a mapping of agent id to
    ``(total_recorded: Decimal, entries: int)``. Raises
    :class:`LedgerSourceUnavailable` until then — never a silent zero.
    """
    _uuid(scope_id, "scope_id")
    wanted = {str(a) for a in agent_ids}
    try:
        from app.agent_controls import store as ak_store  # noqa: F401
    except ImportError as exc:
        raise LedgerSourceUnavailable(
            "AK spend ledger is not on this branch yet: app.agent_controls "
            "(Step AK, branch agent/agent-controls) has not merged. "
            "Recorded spend is unavailable, not zero."
        ) from exc
    reader = getattr(ak_store, "spend_by_agent", None)
    if reader is None:
        raise LedgerSourceUnavailable(
            "app.agent_controls merged but exposes no spend_by_agent reader; "
            "recorded spend is unavailable, not zero."
        )
    found = await reader(db, agent_ids=wanted, scope_id=scope_id)
    out: dict[str, RecordedSpend] = {}
    for agent_id, value in found.items():
        total, entries = value
        total = _money(total, "total_recorded")
        if not isinstance(entries, int) or isinstance(entries, bool) or entries < 0:
            raise ValueError("entries must be a non-negative int")
        out[str(agent_id)] = RecordedSpend(
            agent_id=str(agent_id), total_recorded=_quantize_money(total), entries=entries
        )
    return out


async def routing_estimates(
    db: AsyncSession,
    *,
    run_ids: Iterable[str],
) -> dict[str, RoutingEstimate]:
    """Pre-run cost estimates per run id from the Step T model router.

    Reader contract for the merge: ``routing_records_for_runs(db, *,
    run_ids)`` in ``app.services.routing_records`` (branch
    ``agent/model-router``) returning a mapping of run id to
    ``(estimated_cost_usd: Decimal | None, model: str | None, agent_id:
    str)``. Raises :class:`LedgerSourceUnavailable` until then.
    """
    wanted = {str(r) for r in run_ids}
    try:
        from app.services import routing_records as router_records  # noqa: F401
    except ImportError as exc:
        raise LedgerSourceUnavailable(
            "T routing records are not on this branch yet: "
            "app.services.routing_records (Step T, branch agent/model-router) "
            "has not merged. Routing estimates are unavailable, not zero."
        ) from exc
    reader = getattr(router_records, "routing_records_for_runs", None)
    if reader is None:
        raise LedgerSourceUnavailable(
            "app.services.routing_records merged but exposes no "
            "routing_records_for_runs reader; routing estimates are "
            "unavailable, not zero."
        )
    found = await reader(db, run_ids=wanted)
    out: dict[str, RoutingEstimate] = {}
    for run_id, value in found.items():
        estimate, model, agent_id = value
        if estimate is not None:
            estimate = _money(estimate, "estimated_cost_usd")
        out[str(run_id)] = RoutingEstimate(
            run_id=str(run_id),
            agent_id=str(agent_id),
            estimated_cost_usd=None if estimate is None else _quantize_money(estimate),
            model=None if model is None else str(model),
        )
    return out


def reconcile(
    *,
    estimated_by_agent: Mapping[str, Any],
    estimated_runs_by_agent: Mapping[str, int],
    unpriced_by_agent: Mapping[str, int],
    recorded_by_agent: Mapping[str, RecordedSpend] | None,
) -> dict[str, Reconciliation]:
    """Pure reconciliation: Studio estimate vs AK recorded spend, per agent.

    ``recorded_by_agent=None`` means the AK ledger is unavailable — every
    agent reports ``recorded_unavailable``. An agent present in the
    recorded map but absent from estimates (no runs in the window) is
    still reported, with ``estimated_total`` zero.
    """
    estimated_totals: dict[str, Decimal] = {}
    for agent_id, total in estimated_by_agent.items():
        estimated_totals[str(agent_id)] = _quantize_money(_money(total, "estimated_total"))
    agents = set(estimated_totals) | (
        {str(a) for a in recorded_by_agent} if recorded_by_agent is not None else set()
    )
    out: dict[str, Reconciliation] = {}
    for agent_id in sorted(agents):
        estimated = estimated_totals.get(agent_id, Decimal("0"))
        runs = estimated_runs_by_agent.get(agent_id, 0)
        unpriced = unpriced_by_agent.get(agent_id, 0)
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 0:
            raise ValueError("estimated_runs must be a non-negative int")
        if not isinstance(unpriced, int) or isinstance(unpriced, bool) or unpriced < 0:
            raise ValueError("unpriced_runs must be a non-negative int")
        if recorded_by_agent is None:
            out[agent_id] = Reconciliation(
                agent_id=agent_id,
                estimated_total=estimated,
                recorded_total=None,
                variance=None,
                variance_pct=None,
                status="recorded_unavailable",
                estimated_runs=runs,
                recorded_entries=0,
                unpriced_runs=unpriced,
            )
            continue
        recorded = recorded_by_agent.get(agent_id)
        if recorded is None:
            out[agent_id] = Reconciliation(
                agent_id=agent_id,
                estimated_total=estimated,
                recorded_total=None,
                variance=None,
                variance_pct=None,
                status="no_recorded_spend",
                estimated_runs=runs,
                recorded_entries=0,
                unpriced_runs=unpriced,
            )
            continue
        recorded_total = _quantize_money(_money(recorded.total_recorded, "total_recorded"))
        variance = _quantize_money(estimated - recorded_total)
        if recorded_total > 0:
            variance_pct = ((variance * Decimal(100)) / recorded_total).quantize(_PCT_Q)
        else:
            variance_pct = None
        if variance == 0:
            status = "matched"
        elif variance > 0:
            status = "estimated_higher"
        else:
            status = "recorded_higher"
        out[agent_id] = Reconciliation(
            agent_id=agent_id,
            estimated_total=estimated,
            recorded_total=recorded_total,
            variance=variance,
            variance_pct=variance_pct,
            status=status,
            estimated_runs=runs,
            recorded_entries=recorded.entries,
            unpriced_runs=unpriced,
        )
    return out


def compare_routing_estimates(
    records: Iterable[RunRecord],
    estimates: Mapping[str, RoutingEstimate],
) -> RoutingComparison:
    """Pure comparison: Studio estimate vs T pre-run estimate, same run id.

    Only runs present in both inputs are compared. A run whose routing
    estimate is ``None`` (unpriced at routing time) is not counted as a
    mismatch — it is simply not comparable.
    """
    items = validate_records(records)
    compared = 0
    matched = 0
    mismatched_ids: list[str] = []
    routing_total = Decimal("0")
    studio_total = Decimal("0")
    for record in items:
        estimate = estimates.get(record.id)
        if estimate is None:
            continue
        if estimate.estimated_cost_usd is None or record.estimated_cost_usd is None:
            continue
        compared += 1
        routing_total += Decimal(estimate.estimated_cost_usd)
        studio_total += Decimal(record.estimated_cost_usd)
        if _quantize_money(Decimal(estimate.estimated_cost_usd)) == _quantize_money(
            Decimal(record.estimated_cost_usd)
        ):
            matched += 1
        elif len(mismatched_ids) < _MAX_MISMATCH_IDS:
            mismatched_ids.append(record.id)
    return RoutingComparison(
        runs_compared=compared,
        matched=matched,
        mismatched=compared - matched,
        routing_estimated_total=_quantize_money(routing_total),
        studio_estimated_total=_quantize_money(studio_total),
        mismatched_run_ids=tuple(mismatched_ids),
    )


def _reconciliation_fingerprint(reconciliation: Mapping[str, Reconciliation]) -> str:
    payload = [
        reconciliation[key].as_dict()
        for key in sorted(reconciliation)
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


async def scope_ledger(
    db: AsyncSession,
    *,
    scope_id: UUID,
    owner_ids: Iterable[UUID] | None = None,
    since: datetime | None = None,
    include_ak: bool = True,
    include_routing: bool = True,
) -> ScopeLedger:
    """One scope's cost ledger: figures + reconciliation + provenance.

    Run attribution follows :func:`attributed_owner_ids` unless the
    caller passes ``owner_ids`` explicitly (an override for tests and for
    callers that already resolved attribution; the override is used
    verbatim). Never raises for a missing sibling source (AK / T not
    merged yet) — the source is recorded as unavailable in ``sources``
    and its figures report ``recorded_unavailable`` / ``routing=None``.
    Real database errors still raise.
    """
    scope_id = _uuid(scope_id, "scope_id")
    owners = (
        frozenset(_uuid(o, "owner_ids entry") for o in owner_ids)
        if owner_ids is not None
        else await attributed_owner_ids(db, scope_id)
    )
    records = await studio_run_records(db, scope_id=scope_id, owner_ids=owners, since=since)
    overall = rollup(records)["overall"]
    by_agent = summarize_by_agent(records)
    sources: dict[str, str] = {"studio_runs": "ok"}
    recorded: dict[str, RecordedSpend] | None = None
    if include_ak:
        try:
            agent_ids = await scope_agent_ids(db, member_user_ids=owners)
            recorded = await ak_recorded_spend(db, agent_ids=agent_ids, scope_id=scope_id)
        except LedgerSourceUnavailable as exc:
            sources["ak_spend_ledger"] = f"unavailable: {exc}"
        else:
            sources["ak_spend_ledger"] = "ok"
    else:
        sources["ak_spend_ledger"] = "not_requested"
    routing: RoutingComparison | None = None
    if include_routing:
        try:
            estimates = await routing_estimates(db, run_ids=[r.id for r in records])
        except LedgerSourceUnavailable as exc:
            sources["model_router"] = f"unavailable: {exc}"
        else:
            sources["model_router"] = "ok"
            routing = compare_routing_estimates(records, estimates)
    else:
        sources["model_router"] = "not_requested"
    reconciliation = reconcile(
        estimated_by_agent={a: f.total_cost for a, f in by_agent.items()},
        estimated_runs_by_agent={a: f.runs for a, f in by_agent.items()},
        unpriced_by_agent={a: f.cost_unknown_count for a, f in by_agent.items()},
        recorded_by_agent=recorded,
    )
    recorded_total: Decimal | None = None
    if recorded is not None:
        recorded_total = _quantize_money(
            sum((r.total_recorded for r in recorded.values()), Decimal("0"))
        )
    fingerprint = figures_fingerprint(records) + ":" + _reconciliation_fingerprint(reconciliation)
    return ScopeLedger(
        scope_id=str(scope_id),
        figures=overall,
        by_agent=by_agent,
        reconciliation=reconciliation,
        routing=routing,
        recorded_total=recorded_total,
        sources=sources,
        fingerprint=fingerprint,
    )
