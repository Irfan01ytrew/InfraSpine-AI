"""Step AH slice 5 — drill comparison against AH simulations.

Step G records disruption drills as continuity runs (``bc_runs`` with
``mode='drill'``): a plan of recovery steps with RTO/RPO objectives, and
per-step evidence (when each resource was recovered, how much data was
lost, what service level was reached). This module compares a finished
drill against the matching AH disruption analysis:

* which entities the drill actually exercised vs what the simulation
  predicted (unexpected / missing / uncovered hits);
* whether each step met its RTO objective, in observed minutes;
* how the outcome feeds back into the risk ranking — a drill that fails
  raises the affected nodes' risk, so the next ranking reflects what the
  drill proved. A drill that passes never lowers a risk score: one good
  drill is not proof that the risk is gone.

Everything here is pure except :func:`record_drill_comparison`, which
writes the comparison to the audit trail (slice 6) and the outbox. A
comparison never writes to the graph, never changes a plan, never
approves or executes anything.
"""

from __future__ import annotations

from datetime import datetime

from app.resilience.audit import publish_resilience_event, record_resilience_event

# Risk-score feedback per failed-drill finding. Capped per node so one bad
# drill cannot dominate the ranking.
DELTA_UNEXPECTED_AFFECTED = 10
DELTA_UNCOVERED_HIT = 15
DELTA_RTO_MISSED = 10
MAX_DELTA_PER_NODE = 30
MAX_RISK_SCORE = 100


def _parse_moment(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Not a timestamp: {value!r}.")


def _minutes_between(start, end):
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60, 2)


def drill_from_run(run, tasks, plan):
    """Build a drill record from continuity run / task / plan dicts.

    The inputs are the plain public shapes from
    ``app.continuity.service`` (``report`` / ``dashboard``): ``run`` has
    ``id`` / ``mode`` / ``status`` / ``disruption_at`` / ``closed_at``;
    ``plan`` has ``steps`` (``key`` / ``resource_id`` / ``due_minutes``)
    and ``objective_snapshots`` (``resource_id`` -> ``rto_minutes`` /
    ``rpo_minutes`` / ``minimum_service_percent``); ``tasks`` have
    ``step_key`` / ``status`` / ``evidence`` (``recovered_at`` /
    ``restored_point_at`` / ``service_percent``).

    Snapshot entity ids for continuity resources are ``bc:<node id>``
    (the convention from :mod:`app.resilience.snapshot`), so the observed
    affected set is expressed in snapshot ids and compares directly with
    an AH analysis. A drill that is not closed, or not a drill at all,
    raises — only finished drills feed risk scores.
    """
    if not isinstance(run, dict) or run.get("mode") != "drill":
        raise ValueError("Only continuity runs with mode 'drill' are drills.")
    if run.get("status") != "closed":
        raise ValueError("Only a closed drill can be compared (status must be 'closed').")
    steps = (plan or {}).get("steps") or []
    objectives = (plan or {}).get("objective_snapshots") or {}
    by_key = {step["key"]: step for step in steps if isinstance(step, dict) and step.get("key")}
    disruption_at = _parse_moment(run.get("disruption_at"))
    observed_ids = set()
    observed_steps = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        step = by_key.get(task.get("step_key"))
        if step is None:
            continue
        resource_id = step.get("resource_id")
        entity_id = f"bc:{resource_id}" if resource_id else None
        if entity_id:
            observed_ids.add(entity_id)
        evidence = task.get("evidence") or {}
        recovered_at = _parse_moment(evidence.get("recovered_at"))
        restored_at = _parse_moment(evidence.get("restored_point_at"))
        recovery_minutes = _minutes_between(disruption_at, recovered_at)
        data_loss_minutes = _minutes_between(restored_at, disruption_at)
        objective = objectives.get(str(resource_id)) or objectives.get(resource_id) or {}
        rto_minutes = objective.get("rto_minutes")
        rto_met = (
            recovery_minutes is not None and rto_minutes is not None and recovery_minutes <= rto_minutes
        )
        observed_steps.append(
            {
                "step_key": task.get("step_key"),
                "resource_id": resource_id,
                "entity_id": entity_id,
                "status": task.get("status"),
                "recovery_minutes": recovery_minutes,
                "data_loss_minutes": data_loss_minutes,
                "service_percent": evidence.get("service_percent"),
                "rto_minutes": rto_minutes,
                "rto_met": rto_met,
            }
        )
    return {
        "id": str(run.get("id")),
        "scenario_id": str(run.get("scenario_id")) if run.get("scenario_id") else None,
        "mode": "drill",
        "status": run.get("status"),
        "disruption_at": run.get("disruption_at"),
        "closed_at": run.get("closed_at"),
        "observed": {
            "affected_ids": sorted(observed_ids),
            "steps": observed_steps,
        },
    }


def compare_drill(snapshot, analysis, drill):
    """Compare a finished drill against an AH disruption analysis.

    Returns ``{"drill_id", "scenario_id", "matches", "differences",
    "predicted_count", "observed_count", "summary"}``. Difference types:

    * ``unexpected_affected`` — the drill exercised an entity the
      simulation did not predict;
    * ``missing_affected`` — the simulation predicted an entity the drill
      did not exercise (informational: a drill need not cover everything);
    * ``uncovered_hit`` — the drill hit an entity the analysis listed as
      uncovered (no backup / no alternative / no workaround);
    * ``rto_missed`` — a step took longer than its RTO objective;
    * ``rto_beat`` — a step met its RTO objective (recorded, not a
      difference that fails the comparison).
    """
    if not isinstance(analysis, dict):
        raise ValueError("Analysis must be a dict.")
    if not isinstance(drill, dict) or drill.get("mode") != "drill":
        raise ValueError("Drill must be a drill record (see drill_from_run).")
    predicted = {item["id"] for item in analysis.get("affected") or []}
    uncovered = {entity_id for entity_id in (analysis.get("uncovered") or {})}
    observed = set((drill.get("observed") or {}).get("affected_ids") or [])
    steps = (drill.get("observed") or {}).get("steps") or []
    steps_by_entity: dict[str, list[dict]] = {}
    for step in steps:
        if step.get("entity_id"):
            steps_by_entity.setdefault(step["entity_id"], []).append(step)
    differences = []
    for entity_id in sorted(observed - predicted):
        differences.append(
            {"type": "unexpected_affected", "node_id": entity_id,
             "detail": "The drill exercised an entity the simulation did not predict."}
        )
    for entity_id in sorted(observed & uncovered):
        # Contact alone is not a failure: nearly every affected entity
        # lacks a recorded backup. An uncovered hit counts only when the
        # drill shows the missing cover mattered — the entity's step
        # missed its RTO or never verified.
        failed_here = any(
            (step.get("recovery_minutes") is not None
             and step.get("rto_minutes") is not None
             and step["recovery_minutes"] > step["rto_minutes"])
            or (step.get("status") not in (None, "verified"))
            for step in steps_by_entity.get(entity_id, [])
        )
        if failed_here:
            differences.append(
                {"type": "uncovered_hit", "node_id": entity_id,
                 "detail": "The drill hit an entity the analysis listed as uncovered, and its recovery failed."}
            )
    for entity_id in sorted(predicted - observed):
        differences.append(
            {"type": "missing_affected", "node_id": entity_id,
             "detail": "The simulation predicted this entity; the drill did not exercise it."}
        )
    for step in steps:
        if step.get("recovery_minutes") is None or step.get("rto_minutes") is None:
            continue
        if step["recovery_minutes"] > step["rto_minutes"]:
            differences.append(
                {"type": "rto_missed", "node_id": step.get("entity_id"),
                 "step_key": step.get("step_key"),
                 "detail": (
                     f"Recovery took {step['recovery_minutes']} minutes "
                     f"against an RTO of {step['rto_minutes']} minutes."
                 )}
            )
    rto_beats = [
        step for step in (drill.get("observed") or {}).get("steps") or []
        if step.get("recovery_minutes") is not None
        and step.get("rto_minutes") is not None
        and step["recovery_minutes"] <= step["rto_minutes"]
    ]
    failures = [d for d in differences if d["type"] in {"unexpected_affected", "uncovered_hit", "rto_missed"}]
    summary = [
        f"Drill {drill.get('id')} exercised {len(observed)} entit"
        f"{'y' if len(observed) == 1 else 'ies'}; the simulation predicted {len(predicted)}.",
        f"{len(failures)} failing difference(s), {len(rto_beats)} step(s) met their RTO.",
    ]
    return {
        "drill_id": drill.get("id"),
        "scenario_id": drill.get("scenario_id"),
        "matches": not failures,
        "differences": differences,
        "predicted_count": len(predicted),
        "observed_count": len(observed),
        "summary": summary,
    }


def drill_risk_adjustments(comparison):
    """Risk-score feedback from a drill comparison (pure).

    Failing differences raise the affected node's risk: an unexpected
    entity +10, an uncovered hit +15, a missed RTO +10 — capped at +30
    per node so one bad drill cannot dominate the ranking. Missing
    entities and beaten RTOs change nothing: a drill that did not cover
    an entity is not evidence about it, and one good drill never lowers
    a risk score. Returns ``[{node_id, delta, factors}]`` sorted by node.
    """
    deltas: dict[str, int] = {}
    factors: dict[str, list[str]] = {}
    for difference in comparison.get("differences") or []:
        node_id = difference.get("node_id")
        if not node_id:
            continue
        kind = difference["type"]
        if kind == "unexpected_affected":
            delta, factor = DELTA_UNEXPECTED_AFFECTED, "unexpected_in_drill"
        elif kind == "uncovered_hit":
            delta, factor = DELTA_UNCOVERED_HIT, "uncovered_entity_hit_in_drill"
        elif kind == "rto_missed":
            delta, factor = DELTA_RTO_MISSED, "rto_missed_in_drill"
        else:
            continue
        deltas[node_id] = deltas.get(node_id, 0) + delta
        factors.setdefault(node_id, []).append(factor)
    adjustments = []
    for node_id in sorted(deltas):
        adjustments.append(
            {
                "node_id": node_id,
                "delta": min(deltas[node_id], MAX_DELTA_PER_NODE),
                "factors": sorted(set(factors[node_id])),
            }
        )
    return adjustments


def adjust_ranking(ranked, adjustments):
    """Apply drill adjustments to a risk ranking (pure).

    Each adjustment adds its delta (capped at 100 total) and appends a
    named factor (``failed_drill:+N``) so the change stays explainable.
    The ranking is re-sorted highest-first with ties broken by node id,
    exactly like :func:`app.resilience.impact.rank_risks`.
    """
    by_node = {item["node_id"]: item for item in ranked}
    for adjustment in adjustments or []:
        node = by_node.get(adjustment["node_id"])
        if node is None:
            continue
        delta = adjustment["delta"]
        node = {
            **node,
            "risk_score": min(MAX_RISK_SCORE, node["risk_score"] + delta),
            "factors": [*node.get("factors", []), f"failed_drill:+{delta}"],
        }
        by_node[node["node_id"]] = node
    adjusted = sorted(by_node.values(), key=lambda item: (-item["risk_score"], item["node_id"]))
    return adjusted


async def record_drill_comparison(db, principal, *, scope_id, drill, comparison, adjustments):
    """Record a drill comparison in the audit trail and the outbox.

    The comparison itself never changes a score — the adjustments are
    recorded as the drill's feedback so the next ranking can apply them.
    Returns the recorded payload.
    """
    payload = {
        "drill_id": drill.get("id"),
        "scenario_id": drill.get("scenario_id"),
        "matches": comparison["matches"],
        "differences": len(comparison["differences"]),
        "adjustments": adjustments,
        "scope_id": str(scope_id),
    }
    record_resilience_event(
        db,
        principal,
        "drill_compared",
        target=drill.get("id"),
        details=payload,
    )
    await publish_resilience_event(db, principal, "drill_compared", scope_id, payload)
    return payload
