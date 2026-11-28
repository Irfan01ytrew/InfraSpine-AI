"""What-if engine + drill comparison (Step S slice 5).

Pure functions over a twin snapshot, plus a scenario library for the
three founder questions. No I/O, no database, no network, no secrets.
Simulation only: nothing here approves, dispatches, grants access, or
pays. Amounts are exposure figures ("money at risk"), never payment
instructions. Every run is a dry run by construction (``dry_run`` is
always True on the result); rehearsal mode only rewrites the recovery
plan as numbered practice-only steps.

Engine layout (deliberate, see ``docs/step-s-digital-twin-plan.md`` §3):

- Person/team absence (``employee_absence`` / ``team_absence``)
  delegates to slice 1's :func:`app.digital_twin.impact.simulate_absence`
  and normalizes its result into the common what-if shape below.
- System / supplier / site failure (``system_outage`` /
  ``supplier_failure`` / ``site_unavailable`` / ``cyber_incident`` /
  ``compound``) runs the compact twin-native failure simulation in
  :func:`simulate_failure` — failed / degraded / exposed states, the
  all-providers rule for ``supplies`` / ``serves``, ranked recovery
  options, recovery waves, rehearsal. Its semantics intentionally
  match Step AL's general engine (``app.whatif.simulator``): when the
  AL branch merges, this module's failure engine should delegate to
  ``app.whatif.simulator.simulate`` and this module keeps only the
  scenario library, the service wiring, the drill comparison, and the
  API. The two engines must never diverge in meaning — same states,
  same all-providers rule, same recovery ranking.
- :func:`compare_drill` compares a simulation against a Step G
  recovery drill record (plain dicts — a drill's observed outcome as
  data) and lists every difference; it never re-runs or re-scores
  anything.

Common result shape (all scenario kinds)::

    {
        "scenario": {"kind", "name", "template_id", "seed_ids",
                     "seed_names", "horizon_days"},
        "affected": [{"entity", "state", "severity", "distance",
                      "confidence", "path", "recovery_options",
                      "covered_by"}],
        "failed": [ids], "degraded": [ids], "exposed": [ids],
        "money_at_risk": {"totals": {currency: amount},
                          "batch_count": n, "payment_count": n},
        "recovery_plan": {"waves": [[{"entity_id", "entity_name",
                                      "state", "action", "owner",
                                      "note"}]],
                          "blocked_by_dependency_cycle": [entities],
                          "uncovered": [{"entity", "reason"}]},
        "rehearsal": None | {"dry_run": True, "steps": [...],
                             "side_effects": [], "note": ...},
        "truncated": bool, "dry_run": True,
        "fingerprint": <snapshot fingerprint>,
        "assumptions": [...], "as_of": <snapshot timestamp>,
    }

Absence mapping into that shape: seeds are ``failed`` (unavailable);
an affected entity is ``degraded`` when a usable backup covers it and
``failed`` when it is uncovered. Recovery options for an absence are
``activate_backup`` (when covered) or ``manual_workaround``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

from app.digital_twin.horizon import snapshot_fingerprint
from app.digital_twin.impact import _validate_snapshot, money_at_risk, simulate_absence

DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEPTH = 6

SCENARIO_KINDS = frozenset({
    "employee_absence",
    "team_absence",
    "system_outage",
    "supplier_failure",
    "cyber_incident",
    "site_unavailable",
    "compound",
})
SEED_KINDS = {
    "employee_absence": frozenset({"person"}),
    "team_absence": frozenset({"team"}),
    "system_outage": frozenset({"system", "service"}),
    "supplier_failure": frozenset({"supplier"}),
    "cyber_incident": frozenset({"system", "service"}),
    "site_unavailable": frozenset({"site"}),
    "compound": frozenset(
        {"person", "team", "system", "service", "supplier", "site", "data", "process"}
    ),
}
AUTHORITY_KINDS = frozenset({"owns", "responsible_for", "operates", "approves"})
MONEY_KINDS = frozenset({"invoice_batch", "payment"})
_STATE_RANK = {"failed": 0, "degraded": 1, "exposed": 2}

# Named scenario templates (the scenario library). A template fixes the
# kind and the defaults; the caller supplies the seed entity ids. The
# ids, names, and defaults match Step AL's library so drills, the twin
# UI, and the what-if engine share one vocabulary.
SCENARIOS = {
    "server_down": {
        "kind": "system_outage",
        "name": "Server down",
        "description": "A system or service is unavailable (outage).",
        "default_horizon_days": 1,
    },
    "key_employee_leaves": {
        "kind": "employee_absence",
        "name": "Key employee leaves",
        "description": "A person is unavailable (absence / resignation).",
        "default_horizon_days": 7,
    },
    "supplier_fails": {
        "kind": "supplier_failure",
        "name": "Supplier fails",
        "description": "A supplier stops delivering.",
        "default_horizon_days": 14,
    },
    "cyber_incident": {
        "kind": "cyber_incident",
        "name": "Cyber incident",
        "description": "A system is compromised; connected data may be exposed.",
        "default_horizon_days": 3,
    },
    "site_unavailable": {
        "kind": "site_unavailable",
        "name": "Site unavailable",
        "description": "A site (office / warehouse) cannot be used.",
        "default_horizon_days": 7,
    },
}

ASSUMPTIONS = [
    "Simulation only: dependencies propagate potential impact, not a prediction of certain failure.",
    "A backup_for link is a recorded assignment; it is not an access grant and no access is transferred.",
    "Money at risk is exposure only; the simulator never approves, dispatches, or pays anything.",
    "Only entities and relationships present in the snapshot participate; unknown dependencies may exist.",
    "A rehearsal practises recovery steps; no step is executed and no system is touched.",
]


def _as_decimal(value, *, where):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.") from None
    if amount < 0:
        raise ValueError(f"Amount at {where} must be >= 0, got {value!r}.")
    return amount


def _validate_args(kind, horizon_days, depth):
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"Unknown scenario kind {kind!r}; expected one of {sorted(SCENARIO_KINDS)}.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer.")


def _validate_seeds(nodes, kind, seed_ids):
    if not isinstance(seed_ids, list) or not seed_ids:
        raise ValueError("scenario seed_ids must be a non-empty list of entity ids.")
    if len(set(seed_ids)) != len(seed_ids):
        raise ValueError("scenario seed_ids must not contain duplicates.")
    allowed_seeds = SEED_KINDS[kind]
    for seed_id in seed_ids:
        if not isinstance(seed_id, str) or seed_id not in nodes:
            raise ValueError(f"Unknown seed id in snapshot: {seed_id!r}.")
        if nodes[seed_id]["kind"] not in allowed_seeds:
            raise ValueError(
                f"Scenario {kind!r} cannot seed kind {nodes[seed_id]['kind']!r} ({seed_id!r}); "
                f"allowed seed kinds are {sorted(allowed_seeds)}."
            )


def _edge_view(edge):
    return {
        "relationship_id": edge.get("id"),
        "kind": edge["kind"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "evidence": edge.get("evidence", []),
    }


def _severity(entity, state, has_money):
    """Explainable severity: state + money at risk + entity kind."""
    if state == "failed" and (has_money or entity["kind"] in ("process", "invoice_batch", "payment")):
        return "critical"
    if state == "failed":
        return "high"
    if state == "degraded":
        return "medium"
    return "low"


def _entity_owner(nodes, edges, target_id):
    """Best-known human owner of an entity (authority holder, else 'operations')."""
    owners = sorted(
        {e["source_id"] for e in edges
         if e["kind"] in AUTHORITY_KINDS and e["target_id"] == target_id
         and nodes[e["source_id"]]["kind"] == "person"},
        key=lambda sid: (nodes[sid].get("name", ""), sid),
    )
    if owners:
        return nodes[owners[0]].get("name", owners[0])
    return "operations"


def _recovery_options(nodes, edges, entity, failed, degraded):
    """Ranked recovery options for one affected entity.

    Returns ``(options, uncovered_reason)`` where ``options`` is best
    first and ``uncovered_reason`` is None when any option above the
    manual fallback exists. Ranking (same as Step AL):
    activate_backup -> alternate_supplier -> restore_from_backup ->
    manual_workaround.
    """
    target_id = entity["id"]
    required = {str(s).casefold() for s in entity.get("attributes", {}).get("required_skills", []) or []}
    options = []

    recorded = [e for e in edges if e["kind"] == "backup_for" and e["target_id"] == target_id]
    eligible = []
    present_but_unskilled = False
    for edge in recorded:
        backup_id = edge["source_id"]
        backup = nodes[backup_id]
        if backup_id in failed or backup["kind"] != "person":
            continue
        skills = {str(s).casefold() for s in backup.get("attributes", {}).get("skills", []) or []}
        if required <= skills:
            eligible.append(backup)
        else:
            present_but_unskilled = True
    eligible.sort(key=lambda b: (b.get("name", ""), b["id"]))
    if eligible:
        backup = eligible[0]
        options.append({
            "action": "activate_backup",
            "owner": backup.get("name", backup["id"]),
            "owner_id": backup["id"],
            "target": target_id,
            "rank": 1,
            "note": f"{backup.get('name', backup['id'])} covers this (recorded backup, skills verified).",
        })

    alternates = sorted(
        {e["source_id"] for e in edges
         if e["kind"] == "supplies" and e["target_id"] == target_id
         and e["source_id"] not in failed and nodes[e["source_id"]]["kind"] == "supplier"},
        key=lambda sid: (nodes[sid].get("name", ""), sid),
    )
    if alternates:
        supplier = nodes[alternates[0]]
        options.append({
            "action": "alternate_supplier",
            "owner": supplier.get("name", supplier["id"]),
            "owner_id": supplier["id"],
            "target": target_id,
            "rank": 2,
            "note": f"Reroute supply through {supplier.get('name', supplier['id'])}.",
        })

    has_restore = entity["kind"] in ("system", "service", "data", "knowledge") and bool(
        entity.get("attributes", {}).get("restorable_from_backup")
    )
    if has_restore:
        owner = _entity_owner(nodes, edges, target_id)
        options.append({
            "action": "restore_from_backup",
            "owner": owner,
            "owner_id": None,
            "target": target_id,
            "rank": 3,
            "note": "Restore from the last verified backup.",
        })

    owner = _entity_owner(nodes, edges, target_id)
    options.append({
        "action": "manual_workaround",
        "owner": owner,
        "owner_id": None,
        "target": target_id,
        "rank": 4,
        "note": "Operate manually while the failure lasts.",
    })

    if eligible or alternates or has_restore:
        uncovered_reason = None
    elif recorded:
        uncovered_reason = "backup_missing_skills" if present_but_unskilled else "backup_unavailable"
    elif entity["kind"] in ("process", "invoice_batch", "payment"):
        uncovered_reason = "no_alternate_supplier"
    else:
        uncovered_reason = "no_backup_recorded"
    return options, uncovered_reason


def _recovery_waves(nodes, edges, states, options_by_id):
    """Topological recovery waves over ``depends_on``; cycles reported."""
    depends_on_affected = defaultdict(set)
    for edge in edges:
        if edge["kind"] == "depends_on" and edge["source_id"] in states and edge["target_id"] in states:
            depends_on_affected[edge["source_id"]].add(edge["target_id"])
    pending = {t: set(depends_on_affected.get(t, set())) for t in states}
    waves, resolved = [], set()
    while pending:
        ready = sorted(t for t, deps in pending.items() if deps <= resolved)
        if not ready:
            break
        wave_steps = []
        for target in ready:
            best = options_by_id[target][0]
            wave_steps.append({
                "entity_id": target,
                "entity_name": nodes[target].get("name", target),
                "state": states[target],
                "action": best["action"],
                "owner": best["owner"],
                "note": best["note"],
            })
        waves.append(wave_steps)
        resolved.update(ready)
        pending = {t: deps for t, deps in pending.items() if t not in resolved}
    blocked = sorted(pending, key=lambda t: (nodes[t].get("name", ""), t))
    return waves, [nodes[t] for t in blocked]


def _rehearsal(waves):
    steps = []
    number = 0
    for wave_index, wave in enumerate(waves, start=1):
        for step in wave:
            number += 1
            steps.append({
                "number": number,
                "wave": wave_index,
                "entity_id": step["entity_id"],
                "entity_name": step["entity_name"],
                "action": step["action"],
                "owner": step["owner"],
                "note": step["note"],
                "practice_only": True,
            })
    return {
        "dry_run": True,
        "steps": steps,
        "side_effects": [],
        "note": "Rehearsal only: practise these steps in order. No step is executed, no system is touched, no access is granted.",
    }


def build_scenario(template_id, seed_ids, *, horizon_days=None):
    """Build a scenario dict from a library template + seed entity ids.

    ``template_id`` must be a key of :data:`SCENARIOS`; ``seed_ids`` a
    non-empty list of strings. The seed kinds are checked against the
    snapshot later, in :func:`run_whatif` — this builder only validates
    the template and the argument shapes.
    """
    if template_id not in SCENARIOS:
        raise ValueError(f"Unknown scenario template {template_id!r}; expected one of {sorted(SCENARIOS)}.")
    if not isinstance(seed_ids, list) or not seed_ids:
        raise ValueError("seed_ids must be a non-empty list of entity ids.")
    if len(set(seed_ids)) != len(seed_ids):
        raise ValueError("seed_ids must not contain duplicates.")
    for seed_id in seed_ids:
        if not isinstance(seed_id, str) or not seed_id:
            raise ValueError(f"Every seed id must be a non-empty string, got {seed_id!r}.")
    template = SCENARIOS[template_id]
    if horizon_days is None:
        horizon_days = template["default_horizon_days"]
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    return {
        "template_id": template_id,
        "kind": template["kind"],
        "name": template["name"],
        "description": template["description"],
        "seed_ids": list(seed_ids),
        "horizon_days": horizon_days,
    }


def _expand_seeds(nodes, edges, kind, seed_ids):
    """Resolve seed ids to the set of failed node ids.

    A ``team`` seed expands to its person members; every other seed is
    used directly.
    """
    failed = set()
    names = []
    for seed_id in seed_ids:
        entity = nodes[seed_id]
        names.append(entity.get("name", seed_id))
        if entity["kind"] == "team":
            members = sorted(
                e["source_id"]
                for e in edges
                if e["kind"] == "member_of" and e["target_id"] == seed_id
                and nodes[e["source_id"]]["kind"] == "person"
            )
            failed.update(members)
            failed.add(seed_id)
        else:
            failed.add(seed_id)
    return failed, names


def simulate_failure(snapshot, seed_ids, *, kind, horizon_days=DEFAULT_HORIZON_DAYS, depth=DEFAULT_DEPTH):
    """Simulate a system / supplier / site failure over a snapshot.

    Twin-native failure engine for the non-absence scenario kinds
    (see the module docstring for the merge plan with Step AL).
    States: ``failed`` (cannot operate), ``degraded`` (operates
    impaired), ``exposed`` (cyber incident only: data may be
    compromised). A target with several ``supplies`` / ``serves``
    providers fails only when **all** of them have failed; while any
    one still works it is ``degraded`` instead.
    """
    _validate_args(kind, horizon_days, depth)
    nodes, edges = _validate_snapshot(snapshot)
    _validate_seeds(nodes, kind, seed_ids)
    seed_failed, seed_names = _expand_seeds(nodes, edges, kind, seed_ids)
    cyber = kind == "cyber_incident"

    providers = defaultdict(set)
    for edge in edges:
        if edge["kind"] in ("supplies", "serves"):
            providers[(edge["target_id"], edge["kind"])].add(edge["source_id"])
    forward = defaultdict(list)
    for edge in edges:
        edge_kind = edge["kind"]
        if edge_kind in ("depends_on", "located_at"):
            forward[edge["target_id"]].append(
                (edge["source_id"], edge, {"failed": "failed", "degraded": "degraded", "exposed": "degraded"})
            )
        elif edge_kind in ("supplies", "serves"):
            forward[edge["source_id"]].append((edge["target_id"], edge, None))
        elif edge_kind in AUTHORITY_KINDS:
            forward[edge["source_id"]].append(
                (edge["target_id"], edge, {"failed": "failed", "degraded": "degraded", "exposed": "degraded"})
            )

    states, paths, confidences = {}, {}, {}
    queue = deque()
    for seed_id in sorted(seed_failed):
        states[seed_id] = "failed"
        paths[seed_id] = []
        confidences[seed_id] = 1.0
        queue.append(seed_id)

    if cyber:
        for edge in sorted(edges, key=lambda e: (str(e.get("id")), e["kind"])):
            if edge["kind"] not in ("documents", "depends_on", "supplies", "serves"):
                continue
            for endpoint in (edge["source_id"], edge["target_id"]):
                other = edge["target_id"] if endpoint == edge["source_id"] else edge["source_id"]
                if endpoint in seed_failed and other not in states:
                    if nodes[other]["kind"] in ("data", "knowledge"):
                        states[other] = "exposed"
                        paths[other] = [edge]
                        confidences[other] = float(edge.get("confidence", 1.0))
                        queue.append(other)

    truncated = bool(snapshot.get("truncated", False))
    depth_limited = False
    while queue:
        current = queue.popleft()
        current_state = states[current]
        for target, edge, effect in sorted(
            forward[current], key=lambda item: (item[1].get("id") or "", item[0])
        ):
            if target in seed_failed:
                continue
            if effect is None:
                if current_state == "failed":
                    all_providers = providers[(target, edge["kind"])]
                    next_state = (
                        "failed"
                        if all(
                            p in seed_failed or (p in states and states[p] == "failed")
                            for p in all_providers
                        )
                        else "degraded"
                    )
                else:
                    next_state = "degraded"
            else:
                next_state = effect[current_state]
            if cyber and current in seed_failed and next_state == "failed":
                next_state = "degraded"
            new_distance = len(paths[current]) + 1
            if new_distance > depth:
                depth_limited = True
                continue
            if target in states:
                old_distance = len(paths[target])
                old_state = states[target]
                if new_distance > old_distance and _STATE_RANK[next_state] >= _STATE_RANK[old_state]:
                    continue
                if new_distance == old_distance and _STATE_RANK[next_state] >= _STATE_RANK[old_state]:
                    continue
            paths[target] = [*paths[current], edge]
            confidences[target] = min(confidences[current], float(edge.get("confidence", 1.0)))
            states[target] = next_state
            queue.append(target)

    affected_ids = sorted(states, key=lambda t: (len(paths[t]), nodes[t].get("name", ""), t))
    money = money_at_risk([nodes[t] for t in affected_ids])
    money_by_id = {
        entity["id"]: _as_decimal(entity.get("attributes", {}).get("amount", 0), where=f"entity {entity['id']!r}")
        for entity in (nodes[t] for t in affected_ids)
        if entity["kind"] in MONEY_KINDS
    }

    affected = []
    uncovered = []
    failed = sorted(t for t, s in states.items() if s == "failed")
    degraded = sorted(t for t, s in states.items() if s == "degraded")
    exposed = sorted(t for t, s in states.items() if s == "exposed")
    options_by_id = {}
    for target in affected_ids:
        entity = nodes[target]
        options, reason = _recovery_options(nodes, edges, entity, set(failed), set(degraded))
        options_by_id[target] = options
        affected.append({
            "entity": entity,
            "state": states[target],
            "severity": _severity(entity, states[target], target in money_by_id),
            "distance": len(paths[target]),
            "confidence": confidences[target],
            "path": [_edge_view(e) for e in paths[target]],
            "recovery_options": options,
            "covered_by": [{"person_id": o["owner_id"], "name": o["owner"]}
                           for o in options if o["action"] == "activate_backup"],
        })
        if reason is not None:
            uncovered.append({"entity": entity, "reason": reason})

    waves, blocked = _recovery_waves(nodes, edges, states, options_by_id)
    return {
        "seed_names": seed_names,
        "affected": affected,
        "failed": failed,
        "degraded": degraded,
        "exposed": exposed,
        "money_at_risk": money,
        "recovery_plan": {
            "waves": waves,
            "blocked_by_dependency_cycle": blocked,
            "uncovered": uncovered,
        },
        "truncated": truncated or depth_limited,
    }


def _normalize_absence(snapshot, simulation):
    """Map slice 1's absence result into the common what-if shape."""
    nodes = {entity["id"]: entity for entity in snapshot.get("entities", [])}
    failed, degraded = set(simulation["absent_person_ids"]), set()
    affected = []
    options_by_id = {}
    for entry in simulation["affected"]:
        entity = entry["entity"]
        target = entity["id"]
        backups = entry["covered_by"]
        state = "degraded" if backups else "failed"
        if state == "failed":
            failed.add(target)
        else:
            degraded.add(target)
        if backups:
            best = backups[0]
            options = [{
                "action": "activate_backup",
                "owner": best["name"],
                "owner_id": best["person_id"],
                "target": target,
                "rank": 1,
                "note": f"{best['name']} covers this (recorded backup, skills verified).",
            }]
        else:
            options = []
        owner = _entity_owner(nodes, snapshot.get("relationships", []), target)
        options.append({
            "action": "manual_workaround",
            "owner": owner,
            "owner_id": None,
            "target": target,
            "rank": 4,
            "note": "Operate manually while the absence lasts.",
        })
        options_by_id[target] = options
        has_money = entity["kind"] in MONEY_KINDS
        affected.append({
            "entity": entity,
            "state": state,
            "severity": _severity(entity, state, has_money),
            "distance": entry["distance"],
            "confidence": entry["confidence"],
            "path": entry["path"],
            "recovery_options": options,
            "covered_by": backups,
        })
    # Seeds themselves are affected (unavailable) with no path.
    for seed_id in simulation["absent_person_ids"]:
        entity = nodes[seed_id]
        affected.append({
            "entity": entity,
            "state": "failed",
            "severity": _severity(entity, "failed", False),
            "distance": 0,
            "confidence": 1.0,
            "path": [],
            "recovery_options": [{
                "action": "manual_workaround",
                "owner": entity.get("name", seed_id),
                "owner_id": seed_id,
                "target": seed_id,
                "rank": 4,
                "note": "Cover this person's work while they are away.",
            }],
            "covered_by": [],
        })
        options_by_id[seed_id] = affected[-1]["recovery_options"]
    affected.sort(key=lambda e: (e["distance"], e["entity"].get("name", ""), e["entity"]["id"]))
    states = {entry["entity"]["id"]: entry["state"] for entry in affected}
    waves, blocked = _recovery_waves(
        nodes,
        snapshot.get("relationships", []),
        {**states, **{s: "failed" for s in simulation["absent_person_ids"]}},
        options_by_id,
    )
    return {
        "seed_names": simulation["absent_names"],
        "affected": affected,
        "failed": sorted(failed),
        "degraded": sorted(degraded),
        "exposed": [],
        "money_at_risk": simulation["money_at_risk"],
        "recovery_plan": {
            "waves": waves,
            "blocked_by_dependency_cycle": blocked,
            "uncovered": simulation["uncovered"],
        },
        "truncated": simulation["truncated"],
    }


def run_whatif(snapshot, scenario, *, depth=DEFAULT_DEPTH, rehearse=False):
    """Run a what-if simulation over a snapshot (side-effect free).

    ``scenario`` is a dict with ``kind`` (one of ``SCENARIO_KINDS``),
    ``seed_ids`` (non-empty list of entity ids) and ``horizon_days``
    (positive int); a dict from :func:`build_scenario` works directly.
    ``depth`` bounds propagation hops (default 6). ``rehearse=True``
    adds a rehearsal block: the recovery plan rewritten as numbered
    practice-only steps. The result is deterministic for the same
    snapshot + scenario, and the run touches nothing but its arguments
    — no database, no approvals, no payments, ever.
    """
    if not isinstance(scenario, dict):
        raise ValueError("scenario must be a dict with 'kind', 'seed_ids' and 'horizon_days'.")
    kind = scenario.get("kind")
    seed_ids = scenario.get("seed_ids")
    horizon_days = scenario.get("horizon_days", DEFAULT_HORIZON_DAYS)
    _validate_args(kind, horizon_days, depth)
    nodes, edges = _validate_snapshot(snapshot)
    _validate_seeds(nodes, kind, seed_ids)

    if kind in ("employee_absence", "team_absence"):
        simulation = simulate_absence(
            snapshot, seed_ids, horizon_days=horizon_days, depth=depth)
        normalized = _normalize_absence(snapshot, simulation)
    else:
        normalized = simulate_failure(
            snapshot, seed_ids, kind=kind, horizon_days=horizon_days, depth=depth)

    rehearsal = _rehearsal(normalized["recovery_plan"]["waves"]) if rehearse else None
    return {
        "scenario": {
            "kind": kind,
            "name": scenario.get("name", kind),
            "template_id": scenario.get("template_id"),
            "seed_ids": list(seed_ids),
            "seed_names": normalized["seed_names"],
            "horizon_days": horizon_days,
        },
        "affected": normalized["affected"],
        "failed": normalized["failed"],
        "degraded": normalized["degraded"],
        "exposed": normalized["exposed"],
        "money_at_risk": normalized["money_at_risk"],
        "recovery_plan": normalized["recovery_plan"],
        "rehearsal": rehearsal,
        "truncated": normalized["truncated"],
        "dry_run": True,
        "fingerprint": snapshot_fingerprint(snapshot),
        "assumptions": list(ASSUMPTIONS),
        "as_of": snapshot.get("as_of"),
    }


def compare_drill(simulation, drill):
    """Compare a simulation against a Step G recovery drill record.

    ``simulation`` is a :func:`run_whatif` result; ``drill`` is a plain
    dict describing what a real drill observed::

        {"drill_id": str, "kind": str, "seed_ids": [str],
         "horizon_days": int,
         "observed_affected": [entity ids],
         "observed_money_at_risk": {"totals": {currency: amount}},
         "observed_recovery_waves": [[entity ids], ...],
         "ran_at": iso string}

    Returns ``{"drill_id", "simulation_fingerprint", "matches",
    "differences", "simulated_only", "observed_only",
    "money_difference", "wave_match"}``. ``matches`` is True only
    when the affected sets, the money totals, and the recovery waves
    all agree. Differences are plain-language strings, most specific
    first. The comparison never re-runs or re-scores anything.
    """
    if not isinstance(simulation, dict) or "affected" not in simulation:
        raise ValueError("simulation must be a run_whatif result dict.")
    if not isinstance(drill, dict):
        raise ValueError("drill must be a dict.")
    for field in ("drill_id", "observed_affected", "observed_money_at_risk",
                  "observed_recovery_waves"):
        if field not in drill:
            raise ValueError(f"drill record needs {field!r}.")
    if not isinstance(drill["observed_affected"], list):
        raise ValueError("drill observed_affected must be a list of entity ids.")
    if not isinstance(drill["observed_recovery_waves"], list):
        raise ValueError("drill observed_recovery_waves must be a list of waves.")
    observed_money = drill["observed_money_at_risk"]
    if not isinstance(observed_money, dict) or not isinstance(observed_money.get("totals"), dict):
        raise ValueError("drill observed_money_at_risk must be a dict with a 'totals' dict.")

    simulated_ids = {entry["entity"]["id"] for entry in simulation["affected"]}
    observed_ids = set(drill["observed_affected"])
    simulated_only = sorted(simulated_ids - observed_ids)
    observed_only = sorted(observed_ids - simulated_ids)

    simulated_money = simulation.get("money_at_risk", {}).get("totals", {})
    money_difference = {}
    for currency in sorted(set(simulated_money) | set(observed_money["totals"])):
        diff = round(float(simulated_money.get(currency, 0))
                     - float(observed_money["totals"].get(currency, 0)), 2)
        if diff:
            money_difference[currency] = diff

    simulated_waves = [[step["entity_id"] for step in wave]
                       for wave in simulation.get("recovery_plan", {}).get("waves", [])]
    observed_waves = [sorted(wave) for wave in drill["observed_recovery_waves"]]
    wave_match = [sorted(wave) for wave in simulated_waves] == observed_waves

    differences = []
    for entity_id in simulated_only:
        differences.append(f"simulation affected {entity_id} but the drill did not")
    for entity_id in observed_only:
        differences.append(f"drill affected {entity_id} but the simulation did not")
    for currency, diff in sorted(money_difference.items()):
        differences.append(
            f"money at risk differs by {diff} {currency} "
            f"(simulation {simulated_money.get(currency, 0)}, drill {observed_money['totals'].get(currency, 0)})")
    if not wave_match:
        differences.append(
            f"recovery waves differ: simulation has {len(simulated_waves)} waves, "
            f"drill has {len(observed_waves)}")

    return {
        "drill_id": drill["drill_id"],
        "simulation_fingerprint": simulation.get("fingerprint"),
        "matches": not differences,
        "differences": differences,
        "simulated_only": simulated_only,
        "observed_only": observed_only,
        "money_difference": money_difference,
        "wave_match": wave_match,
    }


__all__ = [
    "SCENARIOS",
    "SCENARIO_KINDS",
    "build_scenario",
    "compare_drill",
    "run_whatif",
    "simulate_failure",
]
