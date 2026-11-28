"""Scheduled drill-reconciliation cycle (Step K v2 slice 6, owed from slice 5).

:func:`evaluate_reconciliation` is the contract; this module is
the call wiring: a cycle the twin service / worker (or the CLI
in ``scripts/twin_reconcile.py``) runs on a schedule or after a
drill run. The inputs arrive through an injected
``input_provider`` — an async callable returning the next
reconciliation bundle (or ``None`` when no new drill run is
available). The provider is injected because the drill records
live in Step G, which is not merged on this branch yet; at merge
the provider reads the latest Step G drill run + the twin
simulation for it, and nothing here changes.

Conventions (same as the health / forecast watches):

* Fingerprint-gated: a (drill run, simulation fingerprint) pair
  that already has stored findings is skipped — re-running it
  would store nothing new. A pair whose last run matched (no
  findings stored) re-evaluates: the evaluation is idempotent
  and pure, so the re-run is cheap and stores nothing.
* Quiet hours 22:00–07:00 Asia/Karachi: the cycle records
  findings (review-queue rows), it never sends an alert, so
  quiet hours do NOT gate the recording — the result reports
  whether the run happened during quiet hours for observability.
* The cycle never rewrites a drill record or a twin version,
  never approves, dispatches, grants, or pays anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.db.twin_reconciliation_models import TwinReconciliationFinding
from app.digital_twin import reconciliation_records
from app.digital_twin.health_watch import is_quiet_hour

__all__ = ["run_reconciliation_cycle"]


def _check_bundle(bundle):
    """Validate an input-provider bundle; returns the normalized parts."""
    if not isinstance(bundle, dict):
        raise ValueError("reconciliation bundle must be a dict.")
    simulation = bundle.get("simulation")
    drill_observation = bundle.get("drill_observation")
    if not isinstance(simulation, dict):
        raise ValueError("reconciliation bundle needs a 'simulation' dict.")
    if not isinstance(drill_observation, dict):
        raise ValueError("reconciliation bundle needs a 'drill_observation' dict.")
    drill_run_id = bundle.get("drill_run_id")
    if isinstance(drill_run_id, UUID):
        run_id = drill_run_id
    elif isinstance(drill_run_id, str) and drill_run_id:
        try:
            run_id = UUID(drill_run_id)
        except ValueError as exc:
            raise ValueError(f"drill_run_id must be a UUID, got {drill_run_id!r}.") from exc
    else:
        raise ValueError("reconciliation bundle needs a 'drill_run_id' UUID.")
    g_comparison = bundle.get("g_comparison")
    if g_comparison is not None and not isinstance(g_comparison, dict):
        raise ValueError("reconciliation bundle 'g_comparison' must be a dict or null.")
    scenario_kind = bundle.get("scenario_kind")
    if scenario_kind is not None and (not isinstance(scenario_kind, str) or not scenario_kind):
        raise ValueError("reconciliation bundle 'scenario_kind' must be a non-empty string or null.")
    return simulation, drill_observation, run_id, g_comparison, scenario_kind


async def run_reconciliation_cycle(db, tenant_id, *, input_provider, now=None):
    """Run one scheduled reconciliation cycle for a tenant.

    ``input_provider`` is an async callable returning the next
    bundle (``simulation``, ``drill_observation``,
    ``drill_run_id``, optional ``g_comparison`` /
    ``scenario_kind``) or ``None`` when no new drill run is
    available. Returns ``{"ran": bool, "reason", ...}`` plus the
    evaluation report when it ran. The caller commits.
    """
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    if not callable(input_provider):
        raise ValueError("input_provider must be an async callable.")
    current = now or datetime.now(UTC)
    bundle = await input_provider()
    if bundle is None:
        return {"ran": False, "reason": "no_new_drill_run",
                "evaluated_at": current.isoformat(),
                "quiet_hours": is_quiet_hour(current)}
    simulation, drill_observation, run_id, g_comparison, scenario_kind = _check_bundle(bundle)
    fingerprint = simulation.get("fingerprint")
    if isinstance(fingerprint, str) and len(fingerprint) == 64:
        already = await db.scalar(
            select(TwinReconciliationFinding.id).where(
                TwinReconciliationFinding.tenant_id == tenant_id,
                TwinReconciliationFinding.drill_run_id == run_id,
                TwinReconciliationFinding.simulation_fingerprint == fingerprint,
            ).limit(1)
        )
        if already is not None:
            return {"ran": False, "reason": "already_reconciled",
                    "drill_run_id": str(run_id),
                    "simulation_fingerprint": fingerprint,
                    "evaluated_at": current.isoformat(),
                    "quiet_hours": is_quiet_hour(current)}
    result = await reconciliation_records.evaluate_reconciliation(
        db, tenant_id,
        simulation=simulation, drill_observation=drill_observation,
        g_comparison=g_comparison, drill_run_id=str(run_id),
        scenario_kind=scenario_kind, now=current,
    )
    return {"ran": True, "reason": "evaluated",
            "evaluated_at": current.isoformat(),
            "quiet_hours": is_quiet_hour(current), **result}
