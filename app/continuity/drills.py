"""Disruption drills and drill/simulation comparison (Step G slice 7).

Five drills, one per disruption kind. A drill is a run with ``mode="drill"``
against a seeded scenario; the comparison measures what actually happened
against what the simulation forecast for the same scenario:

* the simulation forecast says whether the configured disruption duration
  would exceed each resource's RTO/MTPD — no observed outage;
* the drill records the observed recovery time and data loss per resource.

A difference is reported either way — a drill that beats its forecast is
reported too, so a forecast that keeps being wrong gets noticed instead
of quietly staying wrong. Drills write no dispatched
``platform_action_executions``: they reuse the normal run flow (activate,
dispatch, evidence, verify, close).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.continuity import journal, service
from app.continuity.models import Scenario
from app.continuity.schemas import Evidence, ScenarioInput
from app.core.errors import ConflictError

DRILL_KINDS = ("resignation", "absence", "server_outage", "database_failure", "supplier_disruption")
DRILL_LABELS = {
    "resignation": "Resignation drill",
    "absence": "Absence drill",
    "server_outage": "Server outage drill",
    "database_failure": "Database failure drill",
    "supplier_disruption": "Supplier disruption drill",
}
DRILL_DURATION_MINUTES = 120


def pick_drill_targets(nodes) -> dict:
    """First suitable disrupted node per drill kind, from the tenant graph.

    ``nodes`` are plain mappings with id/kind/name/data. Pure: no I/O.
    """
    employees = [n for n in nodes if n["kind"] == "employee"]
    systems = [n for n in nodes if n["kind"] == "system"]
    critical_systems = [n for n in systems if (n.get("data") or {}).get("critical")]
    database_systems = [n for n in systems if "database" in n["name"].casefold()]
    suppliers = [n for n in nodes if n["kind"] == "supplier"]
    return {
        "resignation": str(employees[0]["id"]) if employees else None,
        "absence": str(employees[0]["id"]) if employees else None,
        "server_outage": str((critical_systems or systems)[0]["id"]) if systems else None,
        "database_failure": str((database_systems or systems)[0]["id"]) if systems else None,
        "supplier_disruption": str(suppliers[0]["id"]) if suppliers else None,
    }


def compare_drill_to_simulation(drill, simulation) -> dict:
    """Compare observed drill outcomes with the simulation forecast.

    ``drill``: affected_ids, recovered_ids, and per-resource minutes
    (recovery_minutes, rto_minutes, mtpd_minutes, data_loss_minutes,
    rpo_minutes). ``simulation``: affected_ids and per-resource forecast
    (rto_exceeded, mtpd_exceeded). Pure and deterministic.
    """
    differences = []
    affected = set(drill.get("affected_ids", []))
    recovered = set(drill.get("recovered_ids", []))
    simulated = set(simulation.get("affected_ids", []))
    for node_id in sorted(affected - recovered):
        differences.append({"code": "affected_not_recovered", "resource_id": node_id,
                            "detail": "The simulation said this resource would be affected, but the drill did not recover it."})
    for node_id in sorted(recovered - simulated):
        differences.append({"code": "recovered_not_in_simulation", "resource_id": node_id,
                            "detail": "The drill recovered a resource the simulation did not list as affected."})
    recovery = drill.get("recovery_minutes", {})
    rto = drill.get("rto_minutes", {})
    mtpd = drill.get("mtpd_minutes", {})
    data_loss = drill.get("data_loss_minutes", {})
    rpo = drill.get("rpo_minutes", {})
    forecast = simulation.get("forecast", {})
    for node_id in sorted(recovered):
        minutes = recovery.get(node_id)
        if minutes is None:
            continue
        if node_id in rto and minutes > rto[node_id]:
            differences.append({"code": "rto_missed_in_drill", "resource_id": node_id,
                                "detail": f"Observed recovery {minutes} minutes exceeded the RTO of {rto[node_id]} minutes."})
            if not forecast.get(node_id, {}).get("rto_exceeded", False):
                differences.append({"code": "drill_missed_rto_simulation_met", "resource_id": node_id,
                                    "detail": "The simulation forecast the RTO would hold, but the drill missed it."})
        elif forecast.get(node_id, {}).get("rto_exceeded", False):
            differences.append({"code": "drill_met_rto_simulation_missed", "resource_id": node_id,
                                "detail": "The simulation forecast the RTO would be exceeded, but the drill met it."})
        if node_id in mtpd and minutes > mtpd[node_id]:
            differences.append({"code": "mtpd_exceeded_in_drill", "resource_id": node_id,
                                "detail": f"Observed recovery {minutes} minutes exceeded the MTPD of {mtpd[node_id]} minutes."})
        if node_id in data_loss and data_loss[node_id] is not None and node_id in rpo and data_loss[node_id] > rpo[node_id]:
            differences.append({"code": "data_loss_exceeds_rpo", "resource_id": node_id,
                                "detail": f"Observed data loss {data_loss[node_id]} minutes exceeded the RPO of {rpo[node_id]} minutes."})
    return {"differences": differences, "matches": not differences,
            "affected": len(affected), "recovered": len(recovered), "simulated": len(simulated)}


# ------------------------------------------------------------------------------------------- service

async def seed_drills(db, scope, ctx) -> dict:
    """Create (or return) one drill scenario per disruption kind."""
    scope.require("owner", "planner")
    nodes, _links, _assessments = await service.graph(db, scope)
    targets = pick_drill_targets([{"id": str(n.id), "kind": n.kind, "name": n.name, "data": n.data or {}}
                                  for n in nodes])
    now = datetime.now(UTC)
    created, existing, skipped = [], [], []
    for kind in DRILL_KINDS:
        target = targets[kind]
        if target is None:
            skipped.append({"kind": kind, "reason": "No suitable node in the continuity graph."})
            continue
        row = await db.scalar(select(Scenario).where(
            Scenario.tenant_id == scope.tenant_id, Scenario.source_key == f"drill:{kind}"))
        if row is not None:
            existing.append(service.public(row))
            continue
        data = ScenarioInput(name=DRILL_LABELS[kind], kind=kind, disrupted_ids=[uuid.UUID(target)],
                             duration_minutes=DRILL_DURATION_MINUTES,
                             evidence=[Evidence(reference="continuity-drill-seed",
                                                note="Drill scenario seeded from the continuity graph.",
                                                observed_at=now)])
        created_row = await service.create_scenario(db, scope, data, ctx, source_key=f"drill:{kind}")
        row = await db.get(Scenario, uuid.UUID(created_row["id"]))
        created.append(service.public(row))
    await journal.record(db, scope, ctx, "drills_seeded", scope.tenant_id,
                         {"created": len(created), "existing": len(existing), "skipped": len(skipped)})
    return {"created": created, "existing": existing, "skipped": skipped}


async def drill_comparison(db, scope, run_id: uuid.UUID) -> dict:
    """Compare one drill run with the simulation forecast for its scenario."""
    run, plan = await service.run_for(db, scope, run_id)
    if run.mode != "drill":
        raise ConflictError("Drill comparison applies only to drill runs.")
    report = await service.report(db, scope, run_id)
    scenario = await service.scenario_for(db, scope, run.scenario_id) if run.scenario_id else None
    analysis = (scenario.data.get("analysis") if scenario else {}) or {}
    affected = [item["id"] for item in analysis.get("affected", [])]
    objectives = plan.data.get("objective_snapshots", {})
    forecast = {item["id"]: item.get("forecast") or {}
                for item in analysis.get("affected", []) if item.get("forecast")}
    drill = {"affected_ids": affected, "recovered_ids": [],
             "recovery_minutes": {}, "rto_minutes": {}, "mtpd_minutes": {},
             "data_loss_minutes": {}, "rpo_minutes": {}}
    for task in report["tasks"]:
        resource_id = task["step"]["resource_id"]
        observed = task.get("observed")
        if task["status"] != "verified" or not observed:
            continue
        drill["recovered_ids"].append(resource_id)
        drill["recovery_minutes"][resource_id] = observed.get("recovery_minutes")
        objective = objectives.get(resource_id) or {}
        for key in ("rto_minutes", "mtpd_minutes", "rpo_minutes"):
            if objective.get(key) is not None:
                drill[key][resource_id] = objective[key]
        drill["data_loss_minutes"][resource_id] = observed.get("data_loss_minutes")
    comparison = compare_drill_to_simulation(
        drill, {"affected_ids": affected, "forecast": forecast,
                "duration_minutes": analysis.get("duration_minutes")})
    return {"run_id": str(run.id), "scenario_id": str(run.scenario_id) if run.scenario_id else None,
            "drill": drill, "simulation": {"affected_ids": affected, "forecast": forecast,
                                           "duration_minutes": analysis.get("duration_minutes")},
            **comparison}
