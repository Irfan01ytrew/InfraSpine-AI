"""Twin what-if: scenario library + delegating dispatcher (Step K v2 slice 4).

There is ONE failure engine after this slice — Step AL's
:func:`app.whatif.simulator.simulate` — and the twin delegates to it.
The twin keeps only its scenario library, the absence wiring, the
drill comparison, and the API shape. The two engines must never
diverge in meaning for failure scenarios: same states, same
all-providers rule, same recovery ranking. That is proven, not
asserted, by the differential test in
``tests/test_digital_twin_whatif_differential.py``: the same snapshot
+ scenario through the Step S twin engine and the Step AL engine
returns byte-identical results for every failure kind.

Routing (deliberate, and the differential test pins it):

- Failure kinds (``system_outage``, ``supplier_failure``,
  ``cyber_incident``, ``site_unavailable``, ``compound``) delegate to
  Step AL's ``simulate``. The twin then enriches the result with the
  two fields AL does not return: the snapshot ``fingerprint`` (the
  drift/horizon fingerprint, so a result is always traceable to the
  twin version it was computed from) and ``covered_by`` per affected
  entry (derived from the ``activate_backup`` recovery options).
- Absence kinds (``employee_absence``, ``team_absence``) do NOT
  delegate to AL: AL treats an absent person as a plain failed node,
  while the twin's absence engine (Step S
  ``app.digital_twin.impact.simulate_absence``) answers the twin's
  own question — what breaks when a person is away, with backup
  coverage turning at-risk work ``degraded`` instead of ``failed``.
  The differential test documents this divergence (a covered process
  is ``degraded`` for the twin and ``failed`` for AL) and asserts the
  dispatcher never routes an absence scenario to AL.

Neither engine lives on this branch yet (Step S and Step AL merge
separately), so both are loaded lazily and injected for tests:

- :func:`run_whatif` takes optional ``engine`` /
  ``absence_engine`` callables. Tests inject the real engines (loaded
  from their branches) or fakes.
- Without an injection it lazily imports ``app.whatif.simulator``
  (failure) and ``app.digital_twin.impact`` (absence) and raises
  :class:`EngineUnavailable` — naming the missing module — when the
  branch has not merged yet, instead of failing obscurely.

Simulation only: nothing here approves, dispatches, grants access, or
pays. Amounts are exposure figures ("money at risk"), never payment
instructions. Every run is a dry run by construction (``dry_run`` is
always True on the result); rehearsal mode only rewrites the recovery
plan as numbered practice-only steps.

Merge notes (see ``docs/step-k-twin-v2-plan.md`` §7): at rebase onto
Step S this file REPLACES the Step S ``app/digital_twin/whatif.py``.
Retired at that point — the twin-native failure engine
(``simulate_failure`` and its helpers ``_recovery_options``,
``_edge_view``, ``_expand_seeds``, ``_validate_seeds``,
``_validate_args``, ``_as_decimal``) — because AL's simulator now
owns them. Kept from Step S, verbatim where possible:
``build_scenario`` / ``SCENARIOS``, ``_normalize_absence`` and its
helpers (``_severity``, ``_entity_owner``, ``_recovery_waves``),
``_rehearsal``, ``compare_drill``, and the ``run_whatif`` result
shape. The Step S tests that import ``simulate_failure`` move to the
differential test and to AL's ``test_whatif_simulator.py`` at rebase;
the ``run_whatif`` / ``compare_drill`` cases keep passing unchanged.
"""

from __future__ import annotations

from collections import defaultdict

from app.digital_twin.drift import snapshot_fingerprint

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
# Failure kinds delegate to Step AL's simulator; absence kinds stay on
# the twin's own absence engine (see the module docstring).
FAILURE_KINDS = frozenset({
    "system_outage",
    "supplier_failure",
    "cyber_incident",
    "site_unavailable",
    "compound",
})
ABSENCE_KINDS = frozenset({"employee_absence", "team_absence"})
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


class EngineUnavailable(RuntimeError):
    """An engine this slice delegates to has not merged yet.

    Raised (instead of an obscure ``ImportError``) when
    :func:`run_whatif` needs Step AL's simulator or Step S's absence
    engine and neither is importable on this branch.
    """


def _validate_args(kind, horizon_days, depth):
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"Unknown scenario kind {kind!r}; expected one of {sorted(SCENARIO_KINDS)}.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer.")


def _load_failure_engine():
    """Step AL's ``simulate`` — lazy, so this module imports on a
    branch where AL has not merged yet."""
    try:
        from app.whatif.simulator import simulate
    except ImportError as exc:
        raise EngineUnavailable(
            "Step AL's failure engine (app.whatif.simulator) is not "
            "available on this branch yet; pass an engine to run_whatif, "
            "or merge Step AL first."
        ) from exc
    return simulate


def _load_absence_engine():
    """Step S's ``simulate_absence`` — lazy, so this module imports on
    a branch where Step S has not merged yet."""
    try:
        from app.digital_twin.impact import simulate_absence
    except ImportError as exc:
        raise EngineUnavailable(
            "Step S's absence engine (app.digital_twin.impact) is not "
            "available on this branch yet; pass an absence_engine to "
            "run_whatif, or merge Step S first."
        ) from exc
    return simulate_absence


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


def list_templates():
    """The scenario library, in plain language, for the UI and the API.

    One entry per template: its id, kind, name, description and the
    default horizon in days.
    """
    return [
        {"template_id": template_id, "kind": spec["kind"], "name": spec["name"],
         "description": spec["description"], "horizon_days": spec["default_horizon_days"]}
        for template_id, spec in SCENARIOS.items()
    ]


def template_name(template_id):
    """Display name for a template id, or ``None`` when unknown."""
    spec = SCENARIOS.get(template_id)
    return spec["name"] if spec else None


def _enrich_failure_result(result, snapshot):
    """Add the twin-public fields Step AL's result does not carry.

    ``fingerprint`` (the drift/horizon fingerprint of the snapshot the
    simulation ran over) and ``covered_by`` per affected entry
    (derived from the ``activate_backup`` recovery options). Returns a
    copy — the engine's result dict is never mutated.
    """
    if not isinstance(result, dict) or "affected" not in result:
        raise ValueError("failure engine must return a simulation result dict with 'affected'.")
    if not isinstance(result.get("scenario"), dict) or "seed_names" not in result["scenario"]:
        raise ValueError("failure engine must return a simulation result dict with 'scenario.seed_names'.")
    enriched = dict(result)
    affected = []
    for entry in result["affected"]:
        entry = dict(entry)
        entry["covered_by"] = [
            {"person_id": option["owner_id"], "name": option["owner"]}
            for option in entry.get("recovery_options", [])
            if option.get("action") == "activate_backup"
        ]
        affected.append(entry)
    enriched["affected"] = affected
    enriched["fingerprint"] = snapshot_fingerprint(snapshot)
    return enriched


def _normalize_absence(snapshot, simulation):
    """Map Step S's absence result into the common what-if shape."""
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


def run_whatif(snapshot, scenario, *, depth=DEFAULT_DEPTH, rehearse=False,
               engine=None, absence_engine=None):
    """Run a what-if simulation over a snapshot (side-effect free).

    ``scenario`` is a dict with ``kind`` (one of ``SCENARIO_KINDS``),
    ``seed_ids`` (non-empty list of entity ids) and ``horizon_days``
    (positive int); a dict from :func:`build_scenario` works directly.
    ``depth`` bounds propagation hops (default 6). ``rehearse=True``
    adds a rehearsal block: the recovery plan rewritten as numbered
    practice-only steps. The result is deterministic for the same
    snapshot + scenario, and the run touches nothing but its arguments
    — no database, no approvals, no payments, ever.

    Failure kinds delegate to Step AL's simulator (``engine`` when
    given, else lazily imported); absence kinds run Step S's absence
    engine (``absence_engine`` when given, else lazily imported).
    Without an injected engine and before the owning branch merges,
    :class:`EngineUnavailable` names the missing module.
    """
    if not isinstance(scenario, dict):
        raise ValueError("scenario must be a dict with 'kind', 'seed_ids' and 'horizon_days'.")
    kind = scenario.get("kind")
    seed_ids = scenario.get("seed_ids")
    horizon_days = scenario.get("horizon_days", DEFAULT_HORIZON_DAYS)
    _validate_args(kind, horizon_days, depth)

    if kind in FAILURE_KINDS:
        failure_engine = engine if engine is not None else _load_failure_engine()
        raw = failure_engine(snapshot, scenario, depth=depth, rehearse=False)
        normalized = _enrich_failure_result(raw, snapshot)
        seed_names = raw["scenario"]["seed_names"]
    else:  # employee_absence / team_absence — the twin's own engine.
        absence_fn = absence_engine if absence_engine is not None else _load_absence_engine()
        simulation = absence_fn(snapshot, seed_ids, horizon_days=horizon_days, depth=depth)
        normalized = _normalize_absence(snapshot, simulation)
        seed_names = normalized["seed_names"]

    rehearsal = _rehearsal(normalized["recovery_plan"]["waves"]) if rehearse else None
    return {
        "scenario": {
            "kind": kind,
            "name": scenario.get("name", kind),
            "template_id": scenario.get("template_id"),
            "seed_ids": list(seed_ids),
            "seed_names": seed_names,
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
    "ABSENCE_KINDS",
    "EngineUnavailable",
    "FAILURE_KINDS",
    "SCENARIOS",
    "SCENARIO_KINDS",
    "build_scenario",
    "compare_drill",
    "list_templates",
    "run_whatif",
    "template_name",
]
