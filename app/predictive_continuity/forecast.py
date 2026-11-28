"""Time-aware predictive-continuity forecast (Step L slice 1).

Pure functions over a plain-dict twin snapshot. No I/O, no database, no
network, no secrets. Advisory only: a forecast is a warning, never an
action — nothing here approves, dispatches, grants access, restores
data, switches a supplier, or pays. Money figures are exposure
("money at risk"), never payment instructions.

What this slice adds on top of Step S (``app.digital_twin.impact`` /
``app.digital_twin.horizon``) and Step K v2 (drift):

- :func:`forecast_unavailability` answers the v2 §5 question for
  **people, teams, systems and suppliers** — Step S only simulates
  person/team absence. A system or supplier going down is a
  first-class forecast root.
- The answer is **process-first**: a stable, numbered list of
  processes ("14 processes") with owners, dependencies, and money at
  risk — not a flat list of affected entities of every kind.
- The **horizon changes the answer**. Step S records ``horizon_days``
  but it does not change the graph math. Here every affected entity
  carries a ``due_status``: ``due_within_horizon`` when its ``due_at``
  falls inside the window or its remaining ``sla_days`` would breach
  inside it, ``at_risk_later`` otherwise, ``no_deadline`` when the
  snapshot gives it no time information. A 1-day forecast and a
  30-day forecast are different answers, on purpose.
- The explanation names **live work items**: workflow runs, invoice
  batches, payments and approvals in flight that are linked to each
  affected process, so a caller can see *which live work* produced
  each impact — not just which graph links did.
- **Visibility is enforced in the pure layer.** Pass
  ``visible_ids`` (the entity ids the caller may see) and hidden
  processes are omitted from the list *and* from every count and
  money total, so a restricted caller cannot infer how many hidden
  processes exist.

The graph math reuses Step S's propagation semantics exactly, so a
person/team forecast agrees with ``simulate_absence`` on the same
snapshot (a differential test is owed at merge time, once Step S is
on the same branch — this branch is based on ``ca956c2`` without the
Step S modules, so this file imports nothing from them and stands
alone; stdlib only).

Snapshot shape (same as the Step S twin snapshot, plus time fields)::

    {
        "entities": [
            {"id": "p-1", "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
             "attributes": {"required_skills": ["approvals"],
                            "due_at": "2026-12-03", "sla_days": 5}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR",
                            "status": "awaiting_approval"}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "pr-1",
             "kind": "approves", "confidence": 1.0, "evidence": []},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",   # optional, passed through
        "truncated": False,                      # optional, passed through
    }

Time attributes, per entity:

- ``due_at`` — ISO date (``"2026-12-03"``) or ISO datetime. The
  entity is due inside the forecast window when ``due_at`` is on or
  before ``now + horizon_days``. Takes precedence over ``sla_days``.
- ``sla_days`` — integer days of service level *remaining* before
  this entity's SLA breaches. The breach falls inside the window
  when ``sla_days <= horizon_days``. (Remaining days, not the SLA
  length: without a start date the length alone cannot place the
  breach.)

Relationship kinds understood here (same as Step S):

- ``owns`` / ``responsible_for`` / ``operates`` / ``approves`` — a
  person, team, system or supplier holds authority over the target.
  If the holder is unavailable, the target is directly at risk, and
  risk also flows from an at-risk holder to what it holds.
- ``depends_on`` — source needs target; risk flows target -> source.
- ``supplies`` — source supplies target; risk flows source -> target.
- ``backup_for`` — source (a person) is the recorded backup for
  target. A backup counts only when the backup person is not absent
  and holds every skill the target requires. A recorded link is never
  an access grant; the gap is reported, not bridged.
- ``member_of`` — source (a person) belongs to target (a team). Used
  only to expand a team absence to its members.

Any other relationship kind is ignored for propagation (it may still
be present in the snapshot for other consumers).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEPTH = 6

# Standard horizons for the always-on standing forecast (slice 2).
STANDARD_HORIZONS = (1, 7, 14, 30)

# Kinds that may be forecast as unavailable.
UNAVAILABLE_KINDS = frozenset({"person", "team", "system", "supplier"})
# Kinds that form the numbered process list.
PROCESS_KIND = "process"
# Kinds that count as live work in flight for the explanation.
LIVE_WORK_KINDS = frozenset({"workflow_run", "invoice_batch", "payment", "approval"})
MONEY_KINDS = frozenset({"invoice_batch", "payment"})

# Edges from a holder to something it holds authority over.
AUTHORITY_KINDS = frozenset({"owns", "responsible_for", "operates", "approves"})
# Kinds that carry risk forward from an at-risk node to its dependents.
FORWARD_KINDS = frozenset({"depends_on", "supplies"}) | AUTHORITY_KINDS

MAX_ENTITIES = 50_000
MAX_RELATIONSHIPS = 100_000

ASSUMPTIONS = [
    "Forecast only: dependencies propagate potential impact, not a prediction of certain failure.",
    "A backup_for link is a recorded assignment; it is not an access grant and no access is transferred.",
    "Money at risk is exposure only; the forecast never approves, dispatches, or pays anything.",
    "due_at / sla_days are taken as given by the snapshot; a missing deadline is reported as no_deadline, never invented.",
    "Only entities and relationships present in the snapshot participate; unknown dependencies may exist.",
]


def snapshot_fingerprint(snapshot):
    """Fingerprint of a twin snapshot.

    SHA-256 over the canonical JSON of ``entities`` + ``relationships``
    (``sort_keys=True``, ``default=str``) — byte-identical to
    ``app.digital_twin.horizon.snapshot_fingerprint`` and to the Step K
    drift fingerprint for the same snapshot, so fingerprints agree once
    those steps merge. A stored ``fingerprint`` field is trusted when
    present and non-empty.
    """
    stored = snapshot.get("fingerprint") if isinstance(snapshot, dict) else None
    if isinstance(stored, str) and stored:
        return stored
    canonical = json.dumps(
        {"entities": snapshot.get("entities", []),
         "relationships": snapshot.get("relationships", [])},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _parse_due_at(value, *, where):
    """Parse an ISO date/datetime string to an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"due_at at {where} must be an ISO date/datetime string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"due_at at {where} is not a valid ISO date/datetime: {value!r}.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict with 'entities' and 'relationships'.")
    entities = snapshot.get("entities")
    relationships = snapshot.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        raise ValueError("Snapshot must contain 'entities' and 'relationships' lists.")
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"Snapshot has {len(entities)} entities, over the {MAX_ENTITIES} cap.")
    if len(relationships) > MAX_RELATIONSHIPS:
        raise ValueError(f"Snapshot has {len(relationships)} relationships, over the {MAX_RELATIONSHIPS} cap.")
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
        try:
            json.dumps(attributes)
        except (TypeError, ValueError):
            raise ValueError(f"Entity {entity_id!r} 'attributes' must be JSON-serializable.") from None
        if entity["kind"] in MONEY_KINDS:
            if "amount" not in attributes:
                raise ValueError(f"Money entity {entity_id!r} needs attributes.amount.")
            _as_decimal(attributes["amount"], where=f"entity {entity_id!r}")
        if "due_at" in attributes:
            _parse_due_at(attributes["due_at"], where=f"entity {entity_id!r}")
        if "sla_days" in attributes:
            sla = attributes["sla_days"]
            if isinstance(sla, bool) or not isinstance(sla, int) or sla < 0:
                raise ValueError(f"sla_days at entity {entity_id!r} must be a non-negative integer.")
        nodes[entity_id] = entity
    seen_edge_ids = set()
    for edge in relationships:
        if not isinstance(edge, dict):
            raise ValueError("Every relationship must be a dict.")
        edge_id = edge.get("id")
        if edge_id is not None:
            if not isinstance(edge_id, str) or not edge_id:
                raise ValueError("Relationship 'id' must be a non-empty string when present.")
            if edge_id in seen_edge_ids:
                raise ValueError(f"Duplicate relationship id in snapshot: {edge_id!r}.")
            seen_edge_ids.add(edge_id)
        source, target, kind = edge.get("source_id"), edge.get("target_id"), edge.get("kind")
        if not isinstance(source, str) or source not in nodes:
            raise ValueError(f"Relationship {edge_id!r} has unknown source_id {source!r}.")
        if not isinstance(target, str) or target not in nodes:
            raise ValueError(f"Relationship {edge_id!r} has unknown target_id {target!r}.")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"Relationship {edge_id!r} needs a non-empty string 'kind'.")
        confidence = edge.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError(f"Relationship {edge_id!r} confidence must be a number.")
        if not 0 <= confidence <= 1:
            raise ValueError(f"Relationship {edge_id!r} confidence must be between 0 and 1.")
    return nodes


def _due_status_for_horizon(entity, *, horizon_days, horizon_end):
    """(due_status, due_at_iso) for one entity against a horizon."""
    attributes = entity.get("attributes", {}) or {}
    if "due_at" in attributes:
        due = _parse_due_at(attributes["due_at"], where=f"entity {entity.get('id')!r}")
        return ("due_within_horizon", due.isoformat()) if due <= horizon_end else ("at_risk_later", due.isoformat())
    if "sla_days" in attributes:
        return ("due_within_horizon", None) if attributes["sla_days"] <= horizon_days else ("at_risk_later", None)
    return ("no_deadline", None)


def _edge_view(edge):
    return {
        "relationship_id": edge.get("id"),
        "kind": edge["kind"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "evidence": edge.get("evidence", []),
    }


def _expand_absent_people(nodes, edges, unavailable_ids):
    """Resolve unavailable person/team ids to the set of absent person ids."""
    absent_people = set()
    for unavailable_id in unavailable_ids:
        entity = nodes[unavailable_id]
        if entity["kind"] == "person":
            absent_people.add(unavailable_id)
        elif entity["kind"] == "team":
            absent_people.update(
                e["source_id"]
                for e in edges
                if e["kind"] == "member_of" and e["target_id"] == unavailable_id
                and nodes[e["source_id"]]["kind"] == "person"
            )
    return absent_people


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


def forecast_unavailability(snapshot, unavailable_ids, *, horizon_days=DEFAULT_HORIZON_DAYS,
                             now=None, visible_ids=None, depth=DEFAULT_DEPTH):
    """Forecast what breaks when ``unavailable_ids`` are out for ``horizon_days``.

    ``unavailable_ids`` is a non-empty list of entity ids whose ``kind``
    is ``person``, ``team``, ``system`` or ``supplier``. ``now`` is the
    forecast moment (aware datetime; a naive datetime is read as UTC).
    ``visible_ids`` is None (caller may see everything) or the set of
    entity ids the caller may see — hidden entities are omitted from
    the process list, the affected list, every count, and every money
    total. The unavailable roots themselves are always named (the
    caller supplied them), but a root outside ``visible_ids`` is
    reported with its id as its name.

    Returns a plain dict (see the module docstring for the shape). The
    same snapshot + unavailable set + horizon always yields the same
    forecast.
    """
    if not isinstance(unavailable_ids, list) or not unavailable_ids:
        raise ValueError("unavailable_ids must be a non-empty list of entity ids.")
    if len(set(unavailable_ids)) != len(unavailable_ids):
        raise ValueError("unavailable_ids must not contain duplicates.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer.")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer.")
    if visible_ids is not None:
        if not isinstance(visible_ids, (set, frozenset, list, tuple)):
            raise ValueError("visible_ids must be None or a set/list of entity ids.")
        visible_ids = set(visible_ids)
        for entity_id in visible_ids:
            if not isinstance(entity_id, str) or not entity_id:
                raise ValueError("visible_ids must contain non-empty strings only.")
    moment = now or datetime.now(UTC)
    if not isinstance(moment, datetime):
        raise ValueError("now must be a datetime.")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    horizon_end = moment + timedelta(days=horizon_days)

    nodes = _validate_snapshot(snapshot)
    edges = snapshot.get("relationships", [])
    for unavailable_id in unavailable_ids:
        if not isinstance(unavailable_id, str) or unavailable_id not in nodes:
            raise ValueError(f"Unknown unavailable id in snapshot: {unavailable_id!r}.")
        if nodes[unavailable_id]["kind"] not in UNAVAILABLE_KINDS:
            raise ValueError(
                f"Cannot forecast unavailability of kind {nodes[unavailable_id]['kind']!r} "
                f"({unavailable_id!r}); only person, team, system and supplier may be unavailable."
            )

    def is_visible(entity_id):
        return visible_ids is None or entity_id in visible_ids

    absent_people = _expand_absent_people(nodes, edges, unavailable_ids)

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

    # Seeds. A failed system/supplier is itself affected (distance 0,
    # failed=True). An absent person makes what they hold at risk; a
    # team expands to its members first.
    paths, confidences, failed = {}, {}, set()
    queue = deque()
    for unavailable_id in unavailable_ids:
        kind = nodes[unavailable_id]["kind"]
        if kind in ("system", "supplier"):
            if unavailable_id not in paths:
                paths[unavailable_id] = []
                confidences[unavailable_id] = 1.0
                failed.add(unavailable_id)
                queue.append(unavailable_id)
    for edge in sorted(edges, key=lambda e: (str(e.get("id")), e["kind"])):
        if edge["kind"] in AUTHORITY_KINDS and edge["source_id"] in absent_people:
            target = edge["target_id"]
            if target in paths:
                continue
            paths[target] = [edge]
            confidences[target] = float(edge.get("confidence", 1.0))
            queue.append(target)

    truncated = bool(snapshot.get("truncated", False))
    depth_limited = False
    while queue:
        current = queue.popleft()
        for target, edge in sorted(forward[current], key=lambda item: (item[1].get("id") or "", item[0])):
            if target in paths:
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

    def coverage_for(target):
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
        if eligible:
            return eligible, None
        if not recorded:
            return [], "no_backup_recorded"
        if present_but_unskilled:
            return [], "backup_missing_skills"
        return [], "backup_absent"

    # Owners per entity: authority holders, or an owner_id/owner_ids attribute.
    owners_by_target = defaultdict(list)
    for edge in edges:
        if edge["kind"] in AUTHORITY_KINDS:
            owners_by_target[edge["target_id"]].append(edge["source_id"])
    for entity_id, entity in nodes.items():
        attributes = entity.get("attributes", {}) or {}
        for key in ("owner_id", "owner_ids"):
            value = attributes.get(key)
            if isinstance(value, str) and value in nodes:
                owners_by_target[entity_id].append(value)
            elif isinstance(value, list):
                owners_by_target[entity_id].extend(v for v in value if isinstance(v, str) and v in nodes)

    # Live-work linkage: affected live-work entities connected to a process.
    live_work_by_process = defaultdict(list)
    for target in paths:
        entity = nodes[target]
        if entity["kind"] not in LIVE_WORK_KINDS:
            continue
        for edge in edges:
            if edge["source_id"] == target or edge["target_id"] == target:
                other = edge["target_id"] if edge["source_id"] == target else edge["source_id"]
                if nodes[other]["kind"] == PROCESS_KIND and other in paths:
                    live_work_by_process[other].append(target)

    affected = []
    uncovered = []
    for target in sorted(paths, key=lambda t: (len(paths[t]), nodes[t].get("name", ""), t)):
        if not is_visible(target):
            continue
        entity = nodes[target]
        covered_by, reason = coverage_for(target)
        due_status, due_at = _due_status_for_horizon(entity, horizon_days=horizon_days, horizon_end=horizon_end)
        entry = {
            "entity": entity,
            "distance": len(paths[target]),
            "confidence": confidences[target],
            "path": [_edge_view(e) for e in paths[target]],
            "covered_by": covered_by,
            "due_status": due_status,
            "due_at": due_at,
            "failed": target in failed,
        }
        affected.append(entry)
        if not covered_by:
            uncovered.append({"entity": entity, "reason": reason})

    affected_ids = {entry["entity"]["id"] for entry in affected}

    # Recovery waves: an affected entity recovers after the affected
    # entities it depends on. Cycles are reported, not silently dropped.
    depends_on_affected = defaultdict(set)
    for edge in edges:
        if edge["kind"] == "depends_on" and edge["source_id"] in affected_ids and edge["target_id"] in affected_ids:
            depends_on_affected[edge["source_id"]].add(edge["target_id"])
    pending = {t: set(depends_on_affected.get(t, set())) for t in affected_ids}
    waves, resolved = [], set()
    while pending:
        ready = sorted(t for t, deps in pending.items() if deps <= resolved)
        if not ready:
            break
        waves.append([nodes[t] for t in ready])
        resolved.update(ready)
        pending = {t: deps for t, deps in pending.items() if t not in resolved}
    blocked = sorted(pending, key=lambda t: (nodes[t].get("name", ""), t))

    # Process-first view: stable, numbered, owners + dependencies +
    # live work + money at risk per process.
    process_entries = [entry for entry in affected if entry["entity"]["kind"] == PROCESS_KIND]
    process_entries.sort(key=lambda e: (e["distance"], e["entity"].get("name", ""), e["entity"]["id"]))
    processes = []
    for number, entry in enumerate(process_entries, start=1):
        entity = entry["entity"]
        entity_id = entity["id"]
        owner_ids = sorted({o for o in owners_by_target.get(entity_id, []) if is_visible(o)})
        dependencies = set()
        for edge in edges:
            if edge["source_id"] == entity_id and edge["kind"] == "depends_on" and edge["target_id"] in affected_ids:
                dependencies.add(edge["target_id"])
            if edge["target_id"] == entity_id and edge["kind"] == "supplies" and edge["source_id"] in affected_ids:
                dependencies.add(edge["source_id"])
        dependency_list = [
            {"id": dep, "name": nodes[dep].get("name", dep), "kind": nodes[dep]["kind"]}
            for dep in sorted(dependencies, key=lambda d: (nodes[d].get("name", ""), d))
        ]
        linked_money = []
        for other_id in affected_ids:
            if nodes[other_id]["kind"] not in MONEY_KINDS:
                continue
            for edge in edges:
                if {edge["source_id"], edge["target_id"]} == {entity_id, other_id}:
                    linked_money.append(nodes[other_id])
                    break
        live_work = [
            {"id": item, "name": nodes[item].get("name", item), "kind": nodes[item]["kind"],
             "status": (nodes[item].get("attributes", {}) or {}).get("status")}
            for item in sorted(
                {i for i in live_work_by_process.get(entity_id, []) if i in affected_ids},
                key=lambda i: (nodes[i]["kind"], nodes[i].get("name", ""), i),
            )
        ]
        processes.append({
            "n": number,
            "id": entity_id,
            "name": entity.get("name", entity_id),
            "owner_ids": owner_ids,
            "owner_names": [nodes[o].get("name", o) for o in owner_ids],
            "dependencies": dependency_list,
            "distance": entry["distance"],
            "confidence": entry["confidence"],
            "money_at_risk": money_at_risk(linked_money),
            "due_status": entry["due_status"],
            "due_at": entry["due_at"],
            "reason": None if entry["covered_by"] else next(
                (u["reason"] for u in uncovered if u["entity"]["id"] == entity_id), None),
            "covered_by": entry["covered_by"],
            "path": entry["path"],
            "live_work_items": live_work,
        })

    due_processes = [p for p in processes if p["due_status"] == "due_within_horizon"]
    visible_affected_entities = [entry["entity"] for entry in affected]
    visible_due_entities = [
        entry["entity"] for entry in affected if entry["due_status"] == "due_within_horizon"
    ]
    return {
        "unavailable": [
            {"id": uid,
             "name": nodes[uid].get("name", uid) if is_visible(uid) else uid,
             "kind": nodes[uid]["kind"]}
            for uid in unavailable_ids
        ],
        "horizon_days": horizon_days,
        "horizon_end": horizon_end.isoformat(),
        "forecast_at": moment.isoformat(),
        "fingerprint": snapshot_fingerprint(snapshot),
        "processes": processes,
        "affected": affected,
        "uncovered": uncovered,
        "money_at_risk": money_at_risk(visible_affected_entities),
        "money_at_risk_due": money_at_risk(visible_due_entities),
        "counts": {
            "processes": len(processes),
            "processes_due_within_horizon": len(due_processes),
            "affected": len(affected),
            "uncovered": len(uncovered),
        },
        "recovery_waves": waves,
        "blocked_by_dependency_cycle": [nodes[t] for t in blocked],
        "truncated": truncated or depth_limited,
        "assumptions": list(ASSUMPTIONS),
        "as_of": snapshot.get("as_of"),
    }


def forecast_summary(forecast):
    """One deterministic line summarising a forecast.

    Counts and money totals only — no attribute values, no names, no
    secrets.
    """
    counts = forecast["counts"]
    money = forecast["money_at_risk"]["totals"]
    money_due = forecast["money_at_risk_due"]["totals"]
    money_text = ", ".join(f"{currency} {amount}" for currency, amount in sorted(money.items())) or "none"
    money_due_text = ", ".join(f"{currency} {amount}" for currency, amount in sorted(money_due.items())) or "none"
    return (
        f"Predictive continuity: {counts['processes']} process(es) affected "
        f"({counts['processes_due_within_horizon']} due within {forecast['horizon_days']} days), "
        f"{counts['uncovered']} uncovered, "
        f"money at risk {money_text} (due {money_due_text})."
    )


__all__ = [
    "ASSUMPTIONS",
    "DEFAULT_DEPTH",
    "DEFAULT_HORIZON_DAYS",
    "LIVE_WORK_KINDS",
    "MONEY_KINDS",
    "STANDARD_HORIZONS",
    "UNAVAILABLE_KINDS",
    "forecast_summary",
    "forecast_unavailability",
    "money_at_risk",
    "snapshot_fingerprint",
]
