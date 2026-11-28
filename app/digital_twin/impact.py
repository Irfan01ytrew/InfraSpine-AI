"""Person-absence dependency-impact simulation for the company digital twin.

Pure functions over a plain-dict twin snapshot. No I/O, no database, no
network, no secrets. Simulation only: nothing here approves, dispatches,
grants access, or pays. Amounts are exposure figures ("money at risk"),
never payment instructions.

The math extends the existing impact calculations instead of replacing
them: ``app.company_graph.intelligence.impact`` answers "what breaks if
this node fails" for systems/outages, and
``app.continuity.intelligence.analyze`` scores BIA on the continuity
graph. This module answers the twin's question — "what breaks if this
*person* (or team) is away for N days?" — including money flow and backup
coverage, over a merged snapshot that later slices will build from live
data (see ``docs/step-s-digital-twin-plan.md``).

Snapshot shape::

    {
        "entities": [
            {"id": "p-1", "kind": "person", "name": "Ayesha",
             "department_id": "d-1",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR",
                            "status": "awaiting_approval",
                            "required_skills": ["approvals"]}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "b-1",
             "kind": "approves", "confidence": 1.0, "evidence": []},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",   # optional, passed through
        "truncated": False,                      # optional, passed through
    }

Relationship kinds understood here:

- ``owns`` / ``responsible_for`` / ``operates`` / ``approves`` — a person
  (or team) holds authority over the target. If the holder is absent, the
  target is directly at risk, and risk also flows from an at-risk holder
  to what it holds.
- ``depends_on`` — source needs target; risk flows target -> source.
- ``supplies`` — source supplies target; risk flows source -> target.
- ``backup_for`` — source (a person) is the recorded backup for target.
  A backup counts only when the backup person is not absent and holds
  every skill the target requires. A recorded link is never an access
  grant; the gap is reported, not bridged.
- ``member_of`` — source (a person) belongs to target (a team). Used only
  to expand a team absence to its members.

Any other relationship kind is ignored for propagation (it may still be
present in the snapshot for other consumers).
"""

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEPTH = 6

# Edges from a person/team to something they hold authority over.
AUTHORITY_KINDS = frozenset({"owns", "responsible_for", "operates", "approves"})
# Kinds that carry risk forward from an at-risk node to its dependents.
FORWARD_KINDS = frozenset({"depends_on", "supplies"}) | AUTHORITY_KINDS
MONEY_KINDS = frozenset({"invoice_batch", "payment"})
ABSENTABLE_KINDS = frozenset({"person", "team"})

ASSUMPTIONS = [
    "Simulation only: dependencies propagate potential impact, not a prediction of certain failure.",
    "Backup suggestions are recorded graph assignments; a backup_for link is not an access grant and no access is transferred.",
    "Money at risk is exposure only; the twin never approves, dispatches, or pays anything.",
    "Only entities and relationships present in the snapshot participate; unknown dependencies may exist.",
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


def _expand_absent(nodes, edges, absent_ids):
    """Resolve absent ids to the set of absent person ids (teams expand)."""
    absent_people = set()
    names = []
    for absent_id in absent_ids:
        entity = nodes[absent_id]
        names.append(entity.get("name", absent_id))
        if entity["kind"] == "person":
            absent_people.add(absent_id)
        elif entity["kind"] == "team":
            members = sorted(
                e["source_id"]
                for e in edges
                if e["kind"] == "member_of" and e["target_id"] == absent_id
                and nodes[e["source_id"]]["kind"] == "person"
            )
            absent_people.update(members)
        else:  # pragma: no cover - guarded by the caller check
            raise ValueError(f"Cannot simulate absence of kind {entity['kind']!r} ({absent_id!r}).")
    return absent_people, names


def _edge_view(edge):
    return {
        "relationship_id": edge.get("id"),
        "kind": edge["kind"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "evidence": edge.get("evidence", []),
    }


def simulate_absence(snapshot, absent_ids, *, horizon_days=DEFAULT_HORIZON_DAYS, depth=DEFAULT_DEPTH):
    """Simulate who and what is hit when people/teams are away.

    ``absent_ids`` is a non-empty list of entity ids whose ``kind`` is
    ``person`` or ``team``. ``horizon_days`` is the absence window the
    caller is asking about (it does not change the graph math — it is
    recorded on the result so a 7-day answer is never mistaken for a
    1-day answer); ``depth`` bounds propagation hops (default 6, same as
    ``app.company_graph.intelligence.impact``).
    """
    if not isinstance(absent_ids, list) or not absent_ids:
        raise ValueError("absent_ids must be a non-empty list of entity ids.")
    if len(set(absent_ids)) != len(absent_ids):
        raise ValueError("absent_ids must not contain duplicates.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer.")

    nodes, edges = _validate_snapshot(snapshot)
    for absent_id in absent_ids:
        if not isinstance(absent_id, str) or absent_id not in nodes:
            raise ValueError(f"Unknown absent id in snapshot: {absent_id!r}.")
        if nodes[absent_id]["kind"] not in ABSENTABLE_KINDS:
            raise ValueError(
                f"Cannot simulate absence of kind {nodes[absent_id]['kind']!r} ({absent_id!r}); "
                "only 'person' or 'team' may be absent."
            )

    absent_people, absent_names = _expand_absent(nodes, edges, absent_ids)
    absent_roots = set(absent_ids) | absent_people

    # Forward adjacency: from an at-risk node, which nodes become at risk?
    forward = defaultdict(list)
    for edge in edges:
        kind = edge["kind"]
        if kind == "depends_on":
            # source depends on target: risk flows target -> source.
            forward[edge["target_id"]].append((edge["source_id"], edge))
        elif kind in ("supplies",) or kind in AUTHORITY_KINDS:
            # source supplies/holds target: risk flows source -> target.
            forward[edge["source_id"]].append((edge["target_id"], edge))

    # Directly at-risk: anything an absent person holds authority over.
    paths, confidences = {}, {}
    queue = deque()
    for edge in sorted(edges, key=lambda e: (str(e.get("id")), e["kind"])):
        if edge["kind"] in AUTHORITY_KINDS and edge["source_id"] in absent_people:
            target = edge["target_id"]
            if target in absent_roots or target in paths:
                continue
            paths[target] = [edge]
            confidences[target] = float(edge.get("confidence", 1.0))
            queue.append(target)

    truncated = bool(snapshot.get("truncated", False))
    depth_limited = False
    while queue:
        current = queue.popleft()
        for target, edge in sorted(forward[current], key=lambda item: (item[1].get("id") or "", item[0])):
            if target in absent_roots or target in paths:
                continue
            if len(paths[current]) >= depth:
                depth_limited = True
                continue
            paths[target] = [*paths[current], edge]
            confidences[target] = min(confidences[current], float(edge.get("confidence", 1.0)))
            queue.append(target)

    # Backup coverage per affected entity.
    backups_by_target = defaultdict(list)
    for edge in edges:
        if edge["kind"] == "backup_for":
            backups_by_target[edge["target_id"]].append(edge)

    affected = []
    uncovered = []
    for target in sorted(paths, key=lambda t: (len(paths[t]), nodes[t].get("name", ""), t)):
        entity = nodes[target]
        required = {str(s).casefold() for s in entity.get("attributes", {}).get("required_skills", []) or []}
        recorded = backups_by_target.get(target, [])
        eligible = []
        present_but_unskilled = False
        for edge in recorded:
            backup_id = edge["source_id"]
            backup = nodes[backup_id]
            if backup_id in absent_people:
                continue
            if backup["kind"] != "person":
                continue
            skills = {str(s).casefold() for s in backup.get("attributes", {}).get("skills", []) or []}
            if required <= skills:
                eligible.append({"person_id": backup_id, "name": backup.get("name", backup_id)})
            else:
                present_but_unskilled = True
        eligible.sort(key=lambda b: (b["name"], b["person_id"]))
        entry = {
            "entity": entity,
            "distance": len(paths[target]),
            "confidence": confidences[target],
            "path": [_edge_view(e) for e in paths[target]],
            "covered_by": eligible,
        }
        affected.append(entry)
        if not eligible:
            if not recorded:
                reason = "no_backup_recorded"
            elif present_but_unskilled:
                reason = "backup_missing_skills"
            else:
                reason = "backup_absent"
            uncovered.append({"entity": entity, "reason": reason})

    # Recovery waves: an affected entity recovers after the affected
    # entities it depends on. Cycles are reported, not silently dropped.
    depends_on_affected = defaultdict(set)
    for edge in edges:
        if edge["kind"] == "depends_on" and edge["source_id"] in paths and edge["target_id"] in paths:
            depends_on_affected[edge["source_id"]].add(edge["target_id"])
    pending = {t: set(depends_on_affected.get(t, set())) for t in paths}
    waves, resolved = [], set()
    while pending:
        ready = sorted(t for t, deps in pending.items() if deps <= resolved)
        if not ready:
            break
        waves.append([nodes[t] for t in ready])
        resolved.update(ready)
        pending = {t: deps for t, deps in pending.items() if t not in resolved}
    blocked = sorted(pending, key=lambda t: (nodes[t].get("name", ""), t))

    money = money_at_risk([entry["entity"] for entry in affected])

    return {
        "absent_ids": list(absent_ids),
        "absent_names": absent_names,
        "absent_person_ids": sorted(absent_people),
        "horizon_days": horizon_days,
        "affected": affected,
        "uncovered": uncovered,
        "money_at_risk": money,
        "recovery_waves": waves,
        "blocked_by_dependency_cycle": [nodes[t] for t in blocked],
        "truncated": truncated or depth_limited,
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
