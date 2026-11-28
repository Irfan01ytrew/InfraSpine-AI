"""Step M (slice 4): capture paths — record decisions from existing events.

A decision record can be captured *from* something that already happened
elsewhere in the system, instead of being written freehand:

- **Governance** — an approved governance decision (an ``ActionRequest``
  whose approval threshold was met) can execute a decision record: the
  record is created with the governance proposal and decision attached
  as evidence (``governance_proposal`` / ``governance_decision``, both
  keyed by the request id). Approval-gated: the request must actually
  have been approved (status ``ready`` or ``executed`` *and* at least
  one ``approve`` decision row — a request that was auto-ready with
  zero required approvals has no decision to capture), and the actor
  must be a member of the governance tenant (checked through the same
  readability rule slice 3 uses, so a non-member gets 404 and learns
  nothing). Never automatic: nothing in the governance decide/execute
  flow calls this — the capture is an explicit API call.
- **Continuity drill** — decisions taken during a drill run are recorded
  with the run attached as evidence (``continuity_drill``, the run id).
  The run must be a drill (``mode == "drill"``) that has started
  (``active`` / ``suspended`` / ``closed`` — a ``pending`` run has not
  run yet, a ``rejected`` one never ran), and the actor must be able to
  see the run's tenant through continuity's own ``scope_for`` check.
  ``list_decisions_for_drill`` is the read side: the decisions taken
  during one drill run.
- **What-if simulation** — a simulation result can be cited as evidence
  (``simulation``, the simulation id). Step AL (what-if) lives on an
  unmerged sibling branch and has no tables in this codebase, so the
  simulation id cannot be verified as existing here; the ref is stored
  as cited and readability stays hidden (``UNWIRED_KINDS``) until AL
  merges — the same honest boundary slice 3 drew.

Every capture delegates to ``record_decision`` (slice 2), so all of its
rules apply unchanged: ``decision.record`` on the scope, consumed
platform approval for sensitive kinds, graph validation, one audit row
and one event row per lifecycle event, append-only history. A capture
never grants access, never changes an approval, never moves money and
never changes a payee. Evidence is references only, never copied text:
the governance decision's reason and the drill run's details are never
copied into the record — the record carries the caller's own
title/rationale and cites the source by id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import access as continuity_access
from app.continuity.models import Run as ContinuityRun
from app.continuity.models import Scenario as ContinuityScenario
from app.core.errors import InvalidInputError, NotFoundError
from app.db.decision_models import DecisionRecordRow
from app.db.models import User
from app.decision_memory.readable import readable_evidence
from app.decision_memory.records import EvidenceRef
from app.decision_memory.service import (
    PERMISSION_READ,
    PERMISSION_RECORD,
    list_decisions,
    record_decision,
)
from app.governance import models as governance_models
from app.platform import access as platform_access
from app.platform.access import Principal

__all__ = [
    "list_decisions_for_drill",
    "record_from_drill",
    "record_from_governance",
    "record_from_simulation",
]

# Run statuses in which a drill has started (or finished): decisions can
# only be taken during a drill that ran. "pending" has not started;
# "rejected" never ran.
_DRILL_STARTED_STATUSES = frozenset({"active", "suspended", "closed"})


async def _user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("Not found.")
    return user


def _latest_decided_at(candidates, fallback: datetime) -> datetime:
    """The latest non-null timestamp among candidates, else the fallback."""
    times = [item for item in candidates if item is not None]
    return max(times) if times else fallback


# ----------------------------------------------------------------------------------------------- governance

async def record_from_governance(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                                 request_id: uuid.UUID,
                                 title: str, rationale: str, kind: str = "other",
                                 decided_by: uuid.UUID | None = None,
                                 decided_at: datetime | None = None,
                                 alternatives_considered: tuple[str, ...] = (),
                                 evidence: tuple[EvidenceRef, ...] = (),
                                 depends_on: tuple[str, ...] = (),
                                 review_at=None, decision_id: str | None = None,
                                 approval_id: uuid.UUID | None = None) -> DecisionRecordRow:
    """Record a decision executed by an approved governance decision.

    The governance request must exist, must have been approved (status
    ``ready`` or ``executed`` and at least one ``approve`` decision row),
    and the actor must be a member of the request's governance tenant
    (404 otherwise — the request is not revealed to outsiders). The
    record cites the proposal and the decision as evidence and defaults
    ``decided_at`` to the latest approval decision. Sensitive kinds need
    the consumed platform approval, exactly as in ``record_decision``.
    ``decision.record`` on the scope is required first, before the source
    system is touched at all (so a caller without the permission gets
    403, never a 404 about the source).
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    request = await db.get(governance_models.ActionRequest, request_id)
    if request is None:
        raise NotFoundError("Not found.")
    if request.status not in ("ready", "executed"):
        raise InvalidInputError(
            f"Governance request {request_id} has not been approved (status {request.status!r}). "
            "Only an approved governance decision can execute a decision record.")
    approve_rows = list(await db.scalars(select(governance_models.Decision).where(
        governance_models.Decision.tenant_id == request.tenant_id,
        governance_models.Decision.request_id == request.id,
        governance_models.Decision.decision == "approve",
    )))
    if not approve_rows:
        raise InvalidInputError(
            f"Governance request {request_id} has no approval decision. "
            "Only an approved governance decision can execute a decision record.")
    # Membership of the governance tenant, through the same readability
    # rule slice 3 uses for this evidence kind (404 for non-members).
    user = await _user_or_404(db, actor.user_id)
    readable = await readable_evidence(
        db, user=user,
        refs=[EvidenceRef(kind="governance_decision", ref=str(request.id), label="x")])
    if ("governance_decision", str(request.id)) not in readable:
        raise NotFoundError("Not found.")
    capture_evidence = (
        EvidenceRef(kind="governance_proposal", ref=str(request.id),
                    label=f"Governance proposal: {request.action}"),
        EvidenceRef(kind="governance_decision", ref=str(request.id),
                    label=f"Governance decision: {request.action}"),
    )
    return await record_decision(
        db, actor, scope_id=scope_id, title=title, rationale=rationale, kind=kind,
        decided_by=decided_by,
        decided_at=decided_at or _latest_decided_at(
            [row.created_at for row in approve_rows], datetime.now(UTC)),
        alternatives_considered=tuple(alternatives_considered),
        evidence=(*capture_evidence, *tuple(evidence)),
        depends_on=tuple(depends_on), review_at=review_at,
        decision_id=decision_id, approval_id=approval_id,
    )


# ----------------------------------------------------------------------------------------------- drill

async def record_from_drill(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                            run_id: uuid.UUID,
                            title: str, rationale: str, kind: str = "other",
                            decided_by: uuid.UUID | None = None,
                            decided_at: datetime | None = None,
                            alternatives_considered: tuple[str, ...] = (),
                            evidence: tuple[EvidenceRef, ...] = (),
                            depends_on: tuple[str, ...] = (),
                            review_at=None, decision_id: str | None = None,
                            approval_id: uuid.UUID | None = None) -> DecisionRecordRow:
    """Record a decision taken during a continuity drill run.

    The run must exist, must be a drill (``mode == "drill"``) that has
    started, and the actor must be able to see the run's tenant through
    continuity's own ``scope_for`` check (404 otherwise). The record
    cites the run as evidence and defaults ``decided_at`` to when the
    run was activated (or created). Sensitive kinds need the consumed
    platform approval, exactly as in ``record_decision``.
    ``decision.record`` on the scope is required first, before the source
    system is touched at all (so a caller without the permission gets
    403, never a 404 about the source).
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    run = await db.get(ContinuityRun, run_id)
    if run is None:
        raise NotFoundError("Not found.")
    if run.mode != "drill":
        raise InvalidInputError(
            f"Continuity run {run_id} is not a drill run (mode {run.mode!r}). "
            "Only drill runs can capture decisions taken during a drill.")
    if run.status not in _DRILL_STARTED_STATUSES:
        raise InvalidInputError(
            f"Drill run {run_id} has not started (status {run.status!r}). "
            "Decisions can only be recorded for a drill that ran.")
    user = await _user_or_404(db, actor.user_id)
    await continuity_access.scope_for(db, run.tenant_id, user)
    label = "Continuity drill run"
    if run.scenario_id is not None:
        scenario = await db.get(ContinuityScenario, run.scenario_id)
        if scenario is not None:
            label = f"Continuity drill: {scenario.name}"[:200]
    return await record_decision(
        db, actor, scope_id=scope_id, title=title, rationale=rationale, kind=kind,
        decided_by=decided_by,
        # The decisions were taken during the run: prefer when it was
        # activated, else when it was created (not the latest of the
        # two — the run was created after it was activated in tests
        # where activated_at is backdated, and "latest" would lie).
        decided_at=decided_at or run.activated_at or run.created_at or datetime.now(UTC),
        alternatives_considered=tuple(alternatives_considered),
        evidence=(EvidenceRef(kind="continuity_drill", ref=str(run.id), label=label),
                  *tuple(evidence)),
        depends_on=tuple(depends_on), review_at=review_at,
        decision_id=decision_id, approval_id=approval_id,
    )


async def list_decisions_for_drill(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                                   run_id: uuid.UUID, include_archived: bool = False,
                                   limit: int = 100) -> list[DecisionRecordRow]:
    """Decisions in a scope that cite a drill run, newest first.

    Needs ``decision.read`` on the scope. The run must exist and the
    actor must be able to see its tenant (404 otherwise — the run and
    the decisions citing it are not revealed to outsiders). Archived
    (superseded / revoked) decisions are listed only with
    ``include_archived``.
    """
    await platform_access.require(db, actor, PERMISSION_READ, scope_id)
    run = await db.get(ContinuityRun, run_id)
    if run is None:
        raise NotFoundError("Not found.")
    user = await _user_or_404(db, actor.user_id)
    await continuity_access.scope_for(db, run.tenant_id, user)
    rows = await list_decisions(db, actor, scope_id=scope_id,
                                include_archived=True, limit=200)
    wanted = {"kind": "continuity_drill", "ref": str(run.id)}
    cited = [row for row in rows
             if any(isinstance(item, dict) and item.get("kind") == wanted["kind"]
                    and item.get("ref") == wanted["ref"] for item in (row.evidence or ()))]
    if not include_archived:
        cited = [row for row in cited if row.status == "active"]
    return cited[:max(1, min(limit, 200))]


# ----------------------------------------------------------------------------------------------- simulation

async def record_from_simulation(db: AsyncSession, actor: Principal, *, scope_id: uuid.UUID,
                                 simulation_id: str, simulation_label: str | None = None,
                                 title: str, rationale: str, kind: str = "other",
                                 decided_by: uuid.UUID | None = None,
                                 decided_at: datetime | None = None,
                                 alternatives_considered: tuple[str, ...] = (),
                                 evidence: tuple[EvidenceRef, ...] = (),
                                 depends_on: tuple[str, ...] = (),
                                 review_at=None, decision_id: str | None = None,
                                 approval_id: uuid.UUID | None = None) -> DecisionRecordRow:
    """Record a decision citing a what-if simulation result as evidence.

    Step AL (what-if) lives on an unmerged sibling branch and has no
    tables in this codebase, so the simulation id cannot be verified as
    existing here — it is stored exactly as cited (a non-empty string
    of at most 512 characters) and the ref stays hidden from readers
    until AL merges and readability can be wired. Sensitive kinds need
    the consumed platform approval, exactly as in ``record_decision``.
    """
    await platform_access.require(db, actor, PERMISSION_RECORD, scope_id)
    clean_id = " ".join(str(simulation_id or "").split())
    if not 1 <= len(clean_id) <= 512:
        raise InvalidInputError("simulation_id must be a non-empty string of at most 512 characters.")
    label = " ".join(str(simulation_label or "").split()) or "What-if simulation result"
    if len(label) > 200:
        raise InvalidInputError("simulation_label must be at most 200 characters.")
    return await record_decision(
        db, actor, scope_id=scope_id, title=title, rationale=rationale, kind=kind,
        decided_by=decided_by, decided_at=decided_at,
        alternatives_considered=tuple(alternatives_considered),
        evidence=(EvidenceRef(kind="simulation", ref=clean_id, label=label),
                  *tuple(evidence)),
        depends_on=tuple(depends_on), review_at=review_at,
        decision_id=decision_id, approval_id=approval_id,
    )
