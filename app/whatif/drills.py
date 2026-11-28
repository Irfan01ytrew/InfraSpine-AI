"""Drill comparison (Step AL slice 4).

Compare a Step G recovery drill's actual steps with a what-if
simulation's predicted recovery plan, and list the differences
(missing steps, extra steps, different owners, different order).

Pure functions over plain dicts: no I/O, no database, no network, no
secrets. The comparison never modifies the drill records or the
simulation — both inputs are read-only, and the returned comparison is
a new structure.

The two sides speak different id spaces, so matching is by
**entity / resource identity**, normalized:

- A simulated recovery step is keyed by the snapshot entity id
  (``bc:<uuid>`` for continuity nodes, ``cg:<uuid>`` for company-graph
  records, plain ids for synthetic snapshots).
- A drill step is keyed by the plan step's ``resource_id`` (the UUID of
  the continuity node the step recovers). Steps without a resource fall
  back to ``step:<step_key>``, which never matches a simulated step
  and is therefore reported as extra — honest, not invented.
- The ``bc:`` / ``cg:`` prefixes are stripped when matching, so
  ``bc:<uuid>`` matches resource ``<uuid>``.

Owners are compared by id when both sides carry one. When only one
side carries an id, the owner is unknown — the pair is never counted
as a mismatch and never guessed (a simulated owner is a display name
such as "operations"; a drill owner is a user UUID).

Differences listed (plan §1 acceptance test 4):

- ``missing`` — predicted steps the drill never practised.
- ``extra`` — drill steps the simulation never predicted.
- ``owner_mismatches`` — matched steps done by a different owner
  than the simulation predicted.
- ``out_of_order`` — matched steps practised in a different relative
  order than the predicted waves.
"""

from __future__ import annotations

import copy

# Snapshot-id prefixes stripped when matching a simulated entity id
# against a drill step's resource id.
_MATCH_PREFIXES = ("bc:", "cg:")


def _match_key(value):
    """Normalize an entity/resource id for matching.

    ``bc:<uuid>`` and ``cg:<uuid>`` collapse to ``<uuid>``; anything
    else passes through unchanged (as a string).
    """
    text = str(value)
    changed = True
    while changed:
        changed = False
        for prefix in _MATCH_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return text


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def normalize_simulated_steps(result):
    """Flatten a :func:`simulate` result's recovery waves into steps.

    Returns the predicted steps in wave order, each a dict with
    ``key`` (the snapshot entity id), ``entity_id``, ``entity_name``,
    ``action``, ``owner``, ``owner_id`` (or ``None``), ``wave``
    (1-based), ``state`` and ``note``.
    """
    _require(isinstance(result, dict), "Simulation result must be a dict.")
    plan = result.get("recovery_plan")
    _require(isinstance(plan, dict), "Simulation result needs a 'recovery_plan' dict.")
    waves = plan.get("waves")
    _require(isinstance(waves, list), "Simulation recovery_plan needs a 'waves' list.")
    steps = []
    for wave_index, wave in enumerate(waves, start=1):
        _require(isinstance(wave, list), f"Recovery wave {wave_index} must be a list.")
        for step in wave:
            _require(isinstance(step, dict), f"Recovery wave {wave_index} steps must be dicts.")
            entity_id = step.get("entity_id")
            _require(isinstance(entity_id, str) and entity_id, "Simulated step needs a non-empty string 'entity_id'.")
            action = step.get("action")
            _require(isinstance(action, str) and action, "Simulated step needs a non-empty string 'action'.")
            owner = step.get("owner")
            _require(isinstance(owner, str) and owner, "Simulated step needs a non-empty string 'owner'.")
            steps.append({
                "key": entity_id,
                "entity_id": entity_id,
                "entity_name": step.get("entity_name", entity_id),
                "action": action,
                "owner": owner,
                "owner_id": step.get("owner_id"),
                "wave": wave_index,
                "state": step.get("state"),
                "note": step.get("note", ""),
            })
    return steps


def normalize_drill_steps(report):
    """Flatten a continuity drill report into steps.

    ``report`` is the dict ``app.continuity.service.report`` returns:
    ``tasks`` is a list of ``{**public(task), "step": plan_step,
    "observed": {...} | None, "overdue": bool, ...}``. Each plan step
    carries ``key``, ``title``, ``kind``, ``owner_id`` and
    ``resource_id``.

    Returns the drill steps in plan order, each a dict with ``key``
    (the step's resource id, or ``step:<step_key>`` when it has none),
    ``step_key``, ``title``, ``action`` (the plan step kind),
    ``owner_id`` (string or ``None``), ``owner`` (``None`` — a
    continuity report carries no owner name), ``status``, ``verified``
    (task status is ``verified``), ``service_percent`` and
    ``recovery_minutes`` (from the verified evidence, or ``None`` when
    unverified).
    """
    _require(isinstance(report, dict), "Drill report must be a dict.")
    tasks = report.get("tasks")
    _require(isinstance(tasks, list), "Drill report needs a 'tasks' list.")
    steps = []
    for task in tasks:
        _require(isinstance(task, dict), "Drill report tasks must be dicts.")
        step = task.get("step")
        _require(isinstance(step, dict), "Drill task needs a 'step' dict (plan step).")
        step_key = step.get("key")
        _require(isinstance(step_key, str) and step_key, "Drill step needs a non-empty string 'key'.")
        resource_id = step.get("resource_id")
        key = str(resource_id) if resource_id else f"step:{step_key}"
        owner_id = step.get("owner_id")
        owner_id = str(owner_id) if owner_id is not None else None
        observed = task.get("observed") or {}
        _require(isinstance(observed, dict), "Drill task 'observed' must be a dict when present.")
        steps.append({
            "key": key,
            "step_key": step_key,
            "title": step.get("title", step_key),
            "action": step.get("kind"),
            "owner_id": owner_id,
            "owner": None,
            "status": task.get("status"),
            "verified": task.get("status") == "verified",
            "service_percent": observed.get("service_percent"),
            "recovery_minutes": observed.get("recovery_minutes"),
            "overdue": bool(task.get("overdue", False)),
        })
    return steps


def _owners_match(simulated, drill):
    """True when two matched steps agree on the owner.

    Compares owner ids when both sides carry one. When only one side
    carries an id, the pair is not comparable — the owner is unknown,
    never counted as a mismatch and never guessed (a simulated owner
    is a display name such as "operations"; a drill owner is a user
    UUID). Names are compared only when neither side carries an id.
    """
    sim_id, drill_id = simulated.get("owner_id"), drill.get("owner_id")
    if sim_id is not None and drill_id is not None:
        return str(sim_id) == str(drill_id)
    if sim_id is not None or drill_id is not None:
        return True
    sim_owner, drill_owner = simulated.get("owner"), drill.get("owner")
    if sim_owner and drill_owner:
        return str(sim_owner) == str(drill_owner)
    return True


def _out_of_order_keys(sim_order, drill_order):
    """Keys whose relative order differs between two orderings.

    ``sim_order`` / ``drill_order`` are the matched keys in each
    side's order. A key is out of order when it participates in any
    inverted pair (a pair whose order flips between the two lists).
    """
    drill_rank = {key: index for index, key in enumerate(drill_order)}
    out_of_order = set()
    for i, left in enumerate(sim_order):
        for right in sim_order[i + 1:]:
            if drill_rank[left] > drill_rank[right]:
                out_of_order.add(left)
                out_of_order.add(right)
    return sorted(out_of_order)


def compare_drill(simulation_steps, drill_steps):
    """Compare predicted steps with practised drill steps.

    Both arguments are the normalized lists from
    :func:`normalize_simulated_steps` / :func:`normalize_drill_steps`.
    Neither input is mutated.

    Returns a dict with ``matched`` (pairs in simulation order),
    ``missing`` (predicted, not practised), ``extra`` (practised, not
    predicted), ``owner_mismatches``, ``out_of_order``,
    ``duplicate_drill_keys`` (a drill key seen more than once — only
    the first occurrence is matched), ``summary`` and plain-language
    ``findings``.
    """
    _require(isinstance(simulation_steps, list), "simulation_steps must be a list.")
    _require(isinstance(drill_steps, list), "drill_steps must be a list.")
    for step in simulation_steps:
        _require(isinstance(step, dict) and isinstance(step.get("key"), str) and step["key"],
                 "Every simulated step needs a non-empty string 'key'.")
    for step in drill_steps:
        _require(isinstance(step, dict) and isinstance(step.get("key"), str) and step["key"],
                 "Every drill step needs a non-empty string 'key'.")

    drill_by_key = {}
    duplicate_drill_keys = []
    for step in drill_steps:
        key = _match_key(step["key"])
        if key in drill_by_key:
            duplicate_drill_keys.append(key)
            continue
        drill_by_key[key] = step

    matched, missing = [], []
    for step in simulation_steps:
        key = _match_key(step["key"])
        drill_step = drill_by_key.get(key)
        if drill_step is None:
            missing.append(copy.deepcopy(step))
            continue
        matched.append({
            "simulated": copy.deepcopy(step),
            "drill": copy.deepcopy(drill_step),
            "owner_matches": _owners_match(step, drill_step),
        })

    matched_keys = {_match_key(pair["simulated"]["key"]) for pair in matched}
    extra = [copy.deepcopy(step) for step in drill_steps
             if _match_key(step["key"]) not in matched_keys]
    owner_mismatches = [pair for pair in matched if not pair["owner_matches"]]
    sim_order = [_match_key(pair["simulated"]["key"]) for pair in matched]
    drill_order = [_match_key(step["key"]) for step in drill_steps
                   if _match_key(step["key"]) in matched_keys]
    out_of_order = _out_of_order_keys(sim_order, drill_order)

    simulated_count = len(simulation_steps)
    drill_count = len(drill_steps)
    matched_count = len(matched)
    verified_count = sum(1 for step in drill_steps if step.get("verified"))
    summary = {
        "simulated_count": simulated_count,
        "drill_count": drill_count,
        "matched_count": matched_count,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "owner_mismatch_count": len(owner_mismatches),
        "out_of_order_count": len(out_of_order),
        "coverage": (matched_count / simulated_count) if simulated_count else None,
        "owner_match_rate": ((matched_count - len(owner_mismatches)) / matched_count) if matched_count else None,
        "drill_verified_count": verified_count,
        "drill_verified_rate": (verified_count / drill_count) if drill_count else None,
    }

    findings = []
    if simulated_count == 0:
        findings.append("The simulation predicted no recovery steps, so there is nothing to compare.")
    elif matched_count == 0:
        findings.append(
            f"None of the {simulated_count} predicted recovery step(s) were practised in the drill.")
    else:
        findings.append(
            f"{matched_count} of {simulated_count} predicted step(s) were practised in the drill "
            f"(coverage {summary['coverage']:.0%}).")
    if missing:
        findings.append(
            f"{len(missing)} predicted step(s) were not practised: "
            + ", ".join(s.get("entity_name", s["key"]) for s in missing) + ".")
    if extra:
        findings.append(
            f"{len(extra)} drill step(s) were not predicted by the simulation: "
            + ", ".join(s.get("title", s["key"]) for s in extra) + ".")
    if owner_mismatches:
        findings.append(
            f"{len(owner_mismatches)} matched step(s) were done by a different owner than predicted: "
            + ", ".join(p["simulated"].get("entity_name", p["simulated"]["key"]) for p in owner_mismatches) + ".")
    if out_of_order:
        findings.append(
            f"{len(out_of_order)} matched step(s) were practised in a different order than predicted: "
            + ", ".join(out_of_order) + ".")
    if duplicate_drill_keys:
        findings.append(
            f"{len(duplicate_drill_keys)} drill key(s) appeared more than once; only the first occurrence was matched: "
            + ", ".join(sorted(set(duplicate_drill_keys))) + ".")

    return {
        "matched": matched,
        "missing": missing,
        "extra": extra,
        "owner_mismatches": owner_mismatches,
        "out_of_order": out_of_order,
        "duplicate_drill_keys": sorted(set(duplicate_drill_keys)),
        "summary": summary,
        "findings": findings,
    }


def compare_drill_to_simulation(simulation_result, drill_report):
    """Compare one simulation result with one drill report.

    Convenience wrapper around :func:`normalize_simulated_steps`,
    :func:`normalize_drill_steps` and :func:`compare_drill`. Adds the
    scenario and run metadata the caller needs to file the result, and
    a note when the run was a real incident rather than a drill
    (comparison still works — the incident is the ground truth — but
    the finding says so plainly).
    """
    _require(isinstance(simulation_result, dict), "Simulation result must be a dict.")
    _require(isinstance(drill_report, dict), "Drill report must be a dict.")
    comparison = compare_drill(
        normalize_simulated_steps(simulation_result),
        normalize_drill_steps(drill_report),
    )
    run = drill_report.get("run") or {}
    scenario = simulation_result.get("scenario") or {}
    comparison["scenario"] = {
        "kind": scenario.get("kind"),
        "name": scenario.get("name"),
        "template_id": scenario.get("template_id"),
        "seed_ids": list(scenario.get("seed_ids") or []),
        "seed_names": list(scenario.get("seed_names") or []),
        "horizon_days": scenario.get("horizon_days"),
    }
    comparison["run"] = {
        "id": str(run.get("id")) if run.get("id") is not None else None,
        "mode": run.get("mode"),
        "status": run.get("status"),
        "verified_steps": drill_report.get("verified_steps"),
        "total_steps": drill_report.get("total_steps"),
    }
    if run.get("mode") not in (None, "drill"):
        comparison["findings"].insert(
            0,
            f"This run was a real {run.get('mode')} (not a drill); the comparison is against what actually happened.")
    return comparison
