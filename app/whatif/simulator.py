"""General what-if scenario simulator for InfraSpine.

Pure functions over a plain-dict twin snapshot. No I/O, no database, no
network, no secrets. Simulation only: nothing here approves, dispatches,
grants access, or pays. Amounts are exposure figures ("money at risk"),
never payment instructions.

This is the general scenario engine of master-plan v2 §8 (Simulation /
What-if Engine) and Step AL. It shares its snapshot shape with
``app.digital_twin.impact`` (Agent 24, Step S) so both engines can run on
one twin snapshot later, but it does not import or duplicate that module:
the twin answers "what breaks if this *person* is away?"; this engine
answers "what breaks if this *system / supplier / site* fails, is
compromised, or is hit together with something else?" — with failed /
degraded / exposed states, ranked recovery options, and rehearsal mode.

Snapshot shape (identical to the digital twin)::

    {
        "entities": [
            {"id": "s-1", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR",
                            "required_skills": ["approvals"]}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "b-1",
             "kind": "approves", "confidence": 1.0, "evidence": []},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",   # optional, passed through
    }

Relationship kinds understood here:

- ``depends_on`` — source needs target; failure flows target -> source.
- ``supplies`` — source supplies target; failure flows source -> target.
  A target with several suppliers of the same kind fails only when
  **all** of them have failed; while any one still works it is
  ``degraded`` instead (an alternate provider keeps it limping).
- ``serves`` — source serves target (system serves process); same
  all-providers rule as ``supplies``.
- ``located_at`` — source is located at target (a site); failure flows
  target -> source, like ``depends_on``.
- ``owns`` / ``responsible_for`` / ``operates`` / ``approves`` — a
  person (or team) holds authority over the target. If the holder fails
  (absent), the target fails with them.
- ``documents`` — source documents target; in a cyber incident the
  documented data is ``exposed`` (see below). Otherwise ignored.
- ``backup_for`` — source (a person) is the recorded backup for target.
  Never carries failure; used for recovery options only. A recorded
  link is never an access grant; the gap is reported, not bridged.
- ``member_of`` — source (a person) belongs to target (a team). Used
  only to expand a team seed to its members.

Any other relationship kind is ignored for propagation (it may still be
present in the snapshot for other consumers).

States: ``failed`` (cannot operate), ``degraded`` (operates impaired),
``exposed`` (cyber incident only: data may be compromised). A failed
state always wins over degraded/exposed when an entity is reached by
several paths; a nearer (shorter-path) state wins over a farther one.
"""

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEPTH = 6

# Scenario kinds and the entity kinds each may take as seeds.
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

# Edges from a holder to something they hold authority over.
AUTHORITY_KINDS = frozenset({"owns", "responsible_for", "operates", "approves"})
# Kinds that carry failure forward from a failed/degraded node.
FORWARD_KINDS = frozenset({"depends_on", "supplies", "serves"}) | AUTHORITY_KINDS
MONEY_KINDS = frozenset({"invoice_batch", "payment"})
# Worse states sort first; used when an entity is reached twice.
_STATE_RANK = {"failed": 0, "degraded": 1, "exposed": 2}

# Named scenario templates (the scenario library). A template fixes the
# kind and the defaults; the caller supplies the seed entity ids.
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


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict with 'entities' and 'relationships'.")
    entities = snapshot.get("entities")
    relationships = snapshot.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        raise ValueError("Snapshot must contain 'entities' and 'relationships' lists.")
    nodes = {}
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError("Every entity must be a dict.")
        entity_id = entity.get("id")
        kind = entity.get("kind")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("Every entity needs a non-empty string 'id'.")
        if entity_id in nodes:
            raise ValueError(f"Duplicate entity id in snapshot: {entity_id!r}.")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"Entity {entity_id!r} needs a non-empty string 'kind'.")
        if not isinstance(entity.get("name", ""), str):
            raise ValueError(f"Entity {entity_id!r} needs a string 'name'.")
        attributes = entity.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError(f"Entity {entity_id!r} 'attributes' must be a dict.")
        if entity["kind"] in MONEY_KINDS:
            if "amount" not in attributes:
                raise ValueError(f"Money entity {entity_id!r} needs attributes.amount.")
            _as_decimal(attributes["amount"], where=f"entity {entity_id!r}")
        nodes[entity_id] = entity
    edges = []
    for edge in relationships:
        if not isinstance(edge, dict):
            raise ValueError("Every relationship must be a dict.")
        source, target, kind = edge.get("source_id"), edge.get("target_id"), edge.get("kind")
        if not isinstance(source, str) or source not in nodes:
            raise ValueError(f"Relationship {edge.get('id')!r} has unknown source_id {source!r}.")
        if not isinstance(target, str) or target not in nodes:
            raise ValueError(f"Relationship {edge.get('id')!r} has unknown target_id {target!r}.")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"Relationship {edge.get('id')!r} needs a non-empty string 'kind'.")
        confidence = edge.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError(f"Relationship {edge.get('id')!r} confidence must be a number.")
        if not 0 <= confidence <= 1:
            raise ValueError(f"Relationship {edge.get('id')!r} confidence must be between 0 and 1.")
        edges.append(edge)
    return nodes, edges


def _validate_args(kind, horizon_days, depth):
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"Unknown scenario kind {kind!r}; expected one of {sorted(SCENARIO_KINDS)}.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer.")


def _expand_seeds(nodes, edges, kind, seed_ids):
    """Resolve seed ids to the set of failed node ids.

    A ``team`` seed expands to its person members; every other seed is
    used directly. Seed kinds are enforced by the caller.
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


def _edge_view(edge):
    return {
        "relationship_id": edge.get("id"),
        "kind": edge["kind"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "evidence": edge.get("evidence", []),
    }


def _severity(entity, state, money_amounts):
    """Explainable severity: state + money at risk + entity kind."""
    if state == "failed" and (money_amounts or entity["kind"] in ("process", "invoice_batch", "payment")):
        return "critical"
    if state == "failed":
        return "high"
    if state == "degraded":
        return "medium"
    return "low"


def _recovery_options(nodes, edges, entity, failed, degraded):
    """Ranked recovery options for one affected entity.

    Returns ``(options, uncovered_reason)`` where ``options`` is a list
    of ``{"action", "owner", "target", "rank", "note"}`` best first, and
    ``uncovered_reason`` is None when any option above the manual
    fallback exists.
    """
    target_id = entity["id"]
    required = {str(s).casefold() for s in entity.get("attributes", {}).get("required_skills", []) or []}
    options = []

    # 1. Activate a recorded backup (eligible: not failed, has skills).
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

    # 2. Alternate supplier (another non-failed supplier supplies it).
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

    # 3. Restore from backup (systems/data only).
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

    # 4. Manual workaround — always available, lowest rank.
    owner = _entity_owner(nodes, edges, target_id)
    options.append({
        "action": "manual_workaround",
        "owner": owner,
        "owner_id": None,
        "target": target_id,
        "rank": 4,
        "note": "Operate manually while the failure lasts.",
    })

    # Uncovered = no option above the manual fallback. The reason names
    # the most specific gap so the owner knows what to fix first.
    if eligible or alternates or has_restore:
        uncovered_reason = None
    elif recorded:
        uncovered_reason = "backup_missing_skills" if present_but_unskilled else "backup_unavailable"
    elif entity["kind"] in ("process", "invoice_batch", "payment"):
        uncovered_reason = "no_alternate_supplier"
    else:
        uncovered_reason = "no_backup_recorded"
    return options, uncovered_reason


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


def build_scenario(template_id, seed_ids, *, horizon_days=None):
    """Build a scenario dict from a library template + seed entity ids.

    ``template_id`` must be a key of ``SCENARIOS``; ``seed_ids`` a
    non-empty list of strings. The seed kinds are checked against the
    snapshot later, in :func:`simulate` — this builder only validates
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


def simulate(snapshot, scenario, *, depth=DEFAULT_DEPTH, rehearse=False):
    """Run a what-if simulation over a snapshot.

    ``scenario`` is a dict with ``kind`` (one of ``SCENARIO_KINDS``),
    ``seed_ids`` (non-empty list of entity ids) and ``horizon_days``
    (positive int); a dict from :func:`build_scenario` works directly.
    ``depth`` bounds propagation hops (default 6). ``rehearse=True``
    adds a rehearsal block: the recovery plan rewritten as numbered
    practice-only steps. The result is deterministic for the same
    snapshot + scenario, and the run is side-effect free by
    construction — this function touches nothing but its arguments.
    """
    if not isinstance(scenario, dict):
        raise ValueError("scenario must be a dict with 'kind', 'seed_ids' and 'horizon_days'.")
    kind = scenario.get("kind")
    seed_ids = scenario.get("seed_ids")
    horizon_days = scenario.get("horizon_days", DEFAULT_HORIZON_DAYS)
    _validate_args(kind, horizon_days, depth)
    if not isinstance(seed_ids, list) or not seed_ids:
        raise ValueError("scenario seed_ids must be a non-empty list of entity ids.")
    if len(set(seed_ids)) != len(seed_ids):
        raise ValueError("scenario seed_ids must not contain duplicates.")

    nodes, edges = _validate_snapshot(snapshot)
    allowed_seeds = SEED_KINDS[kind]
    for seed_id in seed_ids:
        if not isinstance(seed_id, str) or seed_id not in nodes:
            raise ValueError(f"Unknown seed id in snapshot: {seed_id!r}.")
        if nodes[seed_id]["kind"] not in allowed_seeds:
            raise ValueError(
                f"Scenario {kind!r} cannot seed kind {nodes[seed_id]['kind']!r} ({seed_id!r}); "
                f"allowed seed kinds are {sorted(allowed_seeds)}."
            )

    seed_failed, seed_names = _expand_seeds(nodes, edges, kind, seed_ids)
    failed_seeds = set(seed_failed)
    cyber = kind == "cyber_incident"

    # Forward adjacency: from a node in a given state, which nodes does
    # it push to which state? Entries are (target, edge, effect) where
    # effect maps the source state to the target state — except for
    # ``supplies`` / ``serves``, whose effect is dynamic (None): a target
    # fails only when every provider of that kind has failed, and is
    # degraded while any one provider still works.
    providers = defaultdict(set)
    for edge in edges:
        if edge["kind"] in ("supplies", "serves"):
            providers[(edge["target_id"], edge["kind"])].add(edge["source_id"])
    forward = defaultdict(list)
    for edge in edges:
        edge_kind = edge["kind"]
        if edge_kind in ("depends_on", "located_at"):
            # source needs / is located at target: failure flows target -> source.
            forward[edge["target_id"]].append(
                (edge["source_id"], edge, {"failed": "failed", "degraded": "degraded", "exposed": "degraded"})
            )
        elif edge_kind in ("supplies", "serves"):
            # source supplies/serves target: failure flows source -> target.
            forward[edge["source_id"]].append((edge["target_id"], edge, None))
        elif edge_kind in AUTHORITY_KINDS:
            # source holds target: failure flows source -> target.
            forward[edge["source_id"]].append(
                (edge["target_id"], edge, {"failed": "failed", "degraded": "degraded", "exposed": "degraded"})
            )

    # State per affected node, plus the shortest explanatory path and its
    # confidence. Seeds are failed (never exposed). BFS by distance; a
    # nearer state wins, ties keep the worse state.
    states, paths, confidences = {}, {}, {}
    queue = deque()
    for seed_id in sorted(failed_seeds):
        states[seed_id] = "failed"
        paths[seed_id] = []
        confidences[seed_id] = 1.0
        queue.append(seed_id)

    # Cyber incident: data/knowledge directly connected to a compromised
    # seed is exposed, not failed.
    if cyber:
        for edge in sorted(edges, key=lambda e: (str(e.get("id")), e["kind"])):
            if edge["kind"] not in ("documents", "depends_on", "supplies", "serves"):
                continue
            for endpoint in (edge["source_id"], edge["target_id"]):
                other = edge["target_id"] if endpoint == edge["source_id"] else edge["source_id"]
                if endpoint in failed_seeds and other not in states:
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
            if target in failed_seeds:
                continue
            if effect is None:
                # supplies / serves: failed only when every provider of
                # that kind for this target has failed; otherwise degraded.
                if current_state == "failed":
                    all_providers = providers[(target, edge["kind"])]
                    next_state = (
                        "failed"
                        if all(
                            p in failed_seeds or (p in states and states[p] == "failed")
                            for p in all_providers
                        )
                        else "degraded"
                    )
                else:
                    next_state = "degraded"
            else:
                next_state = effect[current_state]
            if cyber and current in failed_seeds and next_state == "failed":
                # Quarantine: dependents of a compromised node are
                # degraded, not failed — the system may still run, but
                # nothing that depends on it can be trusted at full
                # strength. The compromised node itself stays failed.
                next_state = "degraded"
            new_distance = len(paths[current]) + 1
            if new_distance > depth:
                depth_limited = True
                continue
            if target in states:
                old_distance = len(paths[target])
                old_state = states[target]
                # A nearer state always wins; a worse state wins even
                # when it arrives over a longer path (e.g. a second
                # supplier fails after the first one already degraded
                # the target). Anything else is stale — skip it.
                if new_distance > old_distance and _STATE_RANK[next_state] >= _STATE_RANK[old_state]:
                    continue
                if new_distance == old_distance and _STATE_RANK[next_state] >= _STATE_RANK[old_state]:
                    continue
                # Replace the path (nearer, or worse state).
            paths[target] = [*paths[current], edge]
            confidences[target] = min(
                confidences[current], float(edge.get("confidence", 1.0))
            )
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
    for target in affected_ids:
        entity = nodes[target]
        options, reason = _recovery_options(nodes, edges, entity, set(failed), set(degraded))
        entry = {
            "entity": entity,
            "state": states[target],
            "severity": _severity(entity, states[target], target in money_by_id),
            "distance": len(paths[target]),
            "confidence": confidences[target],
            "path": [_edge_view(e) for e in paths[target]],
            "recovery_options": options,
        }
        affected.append(entry)
        if reason is not None:
            uncovered.append({"entity": entity, "reason": reason})

    # Recovery waves: an affected entity recovers after the affected
    # entities it depends on. Cycles are reported, not silently dropped.
    depends_on_affected = defaultdict(set)
    for edge in edges:
        if edge["kind"] == "depends_on" and edge["source_id"] in states and edge["target_id"] in states:
            depends_on_affected[edge["source_id"]].add(edge["target_id"])
    pending = {t: set(depends_on_affected.get(t, set())) for t in states}
    options_by_id = {e["entity"]["id"]: e["recovery_options"] for e in affected}
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

    rehearsal = None
    if rehearse:
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
        rehearsal = {
            "dry_run": True,
            "steps": steps,
            "side_effects": [],
            "note": "Rehearsal only: practise these steps in order. No step is executed, no system is touched, no access is granted.",
        }

    return {
        "scenario": {
            "kind": kind,
            "name": scenario.get("name", kind),
            "template_id": scenario.get("template_id"),
            "seed_ids": list(seed_ids),
            "seed_names": seed_names,
            "horizon_days": horizon_days,
        },
        "affected": affected,
        "failed": failed,
        "degraded": degraded,
        "exposed": exposed,
        "money_at_risk": money,
        "recovery_plan": {
            "waves": waves,
            "blocked_by_dependency_cycle": [nodes[t] for t in blocked],
            "uncovered": uncovered,
        },
        "rehearsal": rehearsal,
        "truncated": truncated or depth_limited,
        "dry_run": True,
        "assumptions": list(ASSUMPTIONS),
        "as_of": snapshot.get("as_of"),
    }


def money_at_risk(entities):
    """Sum amounts of money entities (invoice batches / payments).

    Returns ``{"totals": {currency: amount}, "batch_count": n,
    "payment_count": n}``. Amounts are exposure only.
    """
    totals = defaultdict(Decimal)
    batch_count = payment_count = 0
    for entity in entities or []:
        if entity.get("kind") not in MONEY_KINDS:
            continue
        attributes = entity.get("attributes", {}) or {}
        amount = _as_decimal(attributes.get("amount"), where=f"entity {entity.get('id')!r}")
        currency = attributes.get("currency") or "unspecified"
        totals[str(currency)] += amount
        if entity["kind"] == "invoice_batch":
            batch_count += 1
        else:
            payment_count += 1
    return {
        "totals": {currency: float(total) for currency, total in sorted(totals.items())},
        "batch_count": batch_count,
        "payment_count": payment_count,
    }
