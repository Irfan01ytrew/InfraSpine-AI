"""Company Resilience AI — dependency-impact, risk-scoring and recovery math.

Pure functions over a plain-dict dependency snapshot. No I/O, no database,
no network, no secrets. Advisory only: nothing here approves, dispatches,
grants access, restores data, switches a supplier, or pays. Amounts are
exposure figures ("money at risk"), never payment instructions.

The snapshot shape is the same one Step S (digital twin) uses
(``app/digital_twin/impact.py``)::

    {
        "entities": [
            {"id": "p-1", "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["invoicing", "approvals"],
                            "critical": True}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR",
                            "status": "awaiting_approval",
                            "required_skills": ["approvals"]}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "b-1",
             "kind": "approves", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",   # optional, passed through
    }

Entity kinds: ``person``, ``team``, ``process``, ``system`` (server /
software), ``data``, ``supplier``, ``invoice_batch``, ``payment``.

Relationship kinds understood here:

- ``owns`` / ``responsible_for`` / ``operates`` / ``approves`` — a person
  (or team) holds authority over the target. If the holder is disrupted,
  the target is directly at risk, and risk also flows from an at-risk
  holder to what it holds.
- ``depends_on`` — source needs target; risk flows target -> source.
- ``supplies`` — source supplies target; risk flows source -> target.
- ``backup_for`` — source (a person) is the recorded backup for target.
  A backup counts only when the backup person is not disrupted and holds
  every skill the target requires. Two recorded conventions are
  honoured: an edge onto the entity itself (twin convention) and an
  edge onto a disrupted person who holds authority over the entity
  (continuity convention — "Bilal backs up Ayesha" covers what Ayesha
  owns/approves). A recorded link is never an access grant; the gap is
  reported, not bridged.
- ``member_of`` — source (a person) belongs to target (a team). Used only
  to expand a team disruption to its members.
- ``stores`` — source (a system) stores target (data); a cyber incident
  on the system puts its data at risk.
- ``hosts`` — source (a system) hosts target (a process); a server
  outage on the system puts the hosted process at risk.

Any other relationship kind is ignored for propagation (it may still be
present in the snapshot for other consumers).

Disruption kinds (``score_disruption`` event ``kind``):

- ``person_absence`` — target ids must be people (a team id expands to
  its members).
- ``team_absence`` — target ids must be teams.
- ``server_outage`` — target ids must be systems.
- ``cyber_incident`` — target ids must be systems or data.
- ``supplier_failure`` — target ids must be suppliers.
"""

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEPTH = 6
STALE_DAYS = 30

DISRUPTION_KINDS = frozenset(
    {
        "person_absence",
        "team_absence",
        "server_outage",
        "cyber_incident",
        "supplier_failure",
    }
)

# Targets a disruption kind may name, by entity kind.
DISRUPTION_TARGETS = {
    "person_absence": frozenset({"person", "team"}),
    "team_absence": frozenset({"team"}),
    "server_outage": frozenset({"system"}),
    "cyber_incident": frozenset({"system", "data"}),
    "supplier_failure": frozenset({"supplier"}),
}

# Edges from a person/team/system to something they hold authority over.
AUTHORITY_KINDS = frozenset({"owns", "responsible_for", "operates", "approves"})
# Outgoing edge kinds that carry risk forward from an at-risk node:
# what it supplies / stores / hosts, or holds authority over. Note
# ``depends_on`` is NOT here: a dependency is not put at risk by its
# dependent — risk flows the other way (see _propagate).
FORWARD_KINDS = frozenset({"supplies", "stores", "hosts"}) | AUTHORITY_KINDS
# Incoming edge kinds that carry risk backward: a node that depends on
# an at-risk node is itself at risk. A supplier of an at-risk node is
# NOT at risk — it is a potential alternative (see _recovery_options).
BACKWARD_KINDS = frozenset({"depends_on"})
MONEY_KINDS = frozenset({"invoice_batch", "payment"})
SEVERITIES = ("critical", "high", "medium", "low")

ASSUMPTIONS = [
    "Advisory only: dependencies propagate potential impact, not a prediction of certain failure.",
    "Backup suggestions are recorded graph assignments; a backup_for link is not an access grant and no access is transferred.",
    "Money at risk is exposure only; this module never approves, dispatches, restores, switches a supplier, or pays anything.",
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
        raise ValueError(f"Amount at {where} must not be negative, got {value!r}.")
    return amount


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict.")
    entities = snapshot.get("entities")
    relationships = snapshot.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        raise ValueError("Snapshot must have 'entities' and 'relationships' lists.")
    nodes = {}
    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("id"):
            raise ValueError("Every entity must be a dict with a non-empty 'id'.")
        if entity["id"] in nodes:
            raise ValueError(f"Duplicate entity id: {entity['id']!r}.")
        nodes[entity["id"]] = entity
    for rel in relationships:
        if not isinstance(rel, dict):
            raise ValueError("Every relationship must be a dict.")
        for key in ("source_id", "target_id", "kind"):
            if not rel.get(key):
                raise ValueError(f"Every relationship needs a non-empty {key!r}.")
        if rel["source_id"] not in nodes or rel["target_id"] not in nodes:
            raise ValueError(
                "Relationship references unknown entity: "
                f"{rel['source_id']!r} -> {rel['target_id']!r}."
            )
        confidence = rel.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("Relationship confidence must be a number.")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("Relationship confidence must be between 0 and 1.")
    return nodes, relationships


def _expand_targets(nodes, relationships, target_ids, kind):
    """Expand team ids to members; validate kinds for the disruption."""
    allowed = DISRUPTION_TARGETS[kind]
    expanded = []
    seen = set()
    for target_id in target_ids:
        node = nodes.get(target_id)
        if node is None:
            raise ValueError(f"Unknown target id: {target_id!r}.")
        if node.get("kind") not in allowed:
            raise ValueError(
                f"Disruption {kind!r} cannot target a {node.get('kind')!r} node "
                f"({target_id!r})."
            )
        if node.get("kind") == "team":
            members = sorted(
                rel["source_id"]
                for rel in relationships
                if rel["kind"] == "member_of" and rel["target_id"] == target_id
            )
            for member in members:
                if member not in seen:
                    seen.add(member)
                    expanded.append(member)
            if not members:
                # A team with no recorded members still counts as disrupted.
                if target_id not in seen:
                    seen.add(target_id)
                    expanded.append(target_id)
        else:
            if target_id not in seen:
                seen.add(target_id)
                expanded.append(target_id)
    return expanded


def _out_edges(relationships):
    """Adjacency: node id -> list of (kind, target_id, confidence)."""
    out = defaultdict(list)
    for rel in relationships:
        out[rel["source_id"]].append(
            (rel["kind"], rel["target_id"], float(rel.get("confidence", 1.0)))
        )
    return out


def _in_edges(relationships):
    """Reverse adjacency: node id -> list of (kind, source_id, confidence)."""
    in_edges = defaultdict(list)
    for rel in relationships:
        in_edges[rel["target_id"]].append(
            (rel["kind"], rel["source_id"], float(rel.get("confidence", 1.0)))
        )
    return in_edges


def _propagate(nodes, relationships, seeds, depth=DEFAULT_DEPTH):
    """BFS from the disrupted seeds over impact-carrying edges.

    Returns (affected, truncated) where affected maps node id to
    {"distance", "confidence", "path"} — path is a list of
    {"from", "to", "kind"} edges from a seed to the node.
    """
    out_edges = _out_edges(relationships)
    in_edges = _in_edges(relationships)
    affected = {}
    truncated = False
    queue = deque()
    for seed in seeds:
        affected[seed] = {"distance": 0, "confidence": 1.0, "path": []}
        queue.append(seed)
    while queue:
        current = queue.popleft()
        info = affected[current]
        if info["distance"] >= depth:
            # Anything further out would exceed the depth limit.
            carries = any(
                kind in FORWARD_KINDS
                for kind, _, _ in out_edges.get(current, [])
            ) or any(
                kind in BACKWARD_KINDS
                for kind, _, _ in in_edges.get(current, [])
            )
            if carries:
                truncated = True
            continue
        neighbours = []
        # Forward: risk flows from an at-risk node to what it holds /
        # supplies / stores / hosts.
        for kind, target, confidence in out_edges.get(current, []):
            if kind in FORWARD_KINDS:
                neighbours.append((target, kind, confidence))
        # Backward: risk flows from an at-risk node to the nodes that
        # depend on it.
        for kind, source, confidence in in_edges.get(current, []):
            if kind in BACKWARD_KINDS:
                neighbours.append((source, kind, confidence))
        for target, kind, confidence in neighbours:
            new_distance = info["distance"] + 1
            new_confidence = min(info["confidence"], confidence)
            new_path = info["path"] + [
                {"from": current, "to": target, "kind": kind}
            ]
            existing = affected.get(target)
            if existing is None or (
                new_distance < existing["distance"]
                or (
                    new_distance == existing["distance"]
                    and new_confidence > existing["confidence"]
                )
            ):
                affected[target] = {
                    "distance": new_distance,
                    "confidence": new_confidence,
                    "path": new_path,
                }
                queue.append(target)
    return affected, truncated


def _backup_coverage(nodes, relationships, affected, disrupted_ids):
    """For each affected entity, which recorded backups could cover it.

    A backup counts only when the backup person is not disrupted and
    holds every skill the entity requires. Two recorded conventions are
    honoured (they differ across the twin and continuity graphs):
    a direct ``backup_for`` edge onto the entity, and a ``backup_for``
    edge onto a disrupted person who holds authority over the entity
    ("Bilal backs up Ayesha" covers what Ayesha owns/approves).
    Returns (covered, uncovered) where covered maps entity id ->
    [backup person ids] and uncovered maps entity id -> reason.
    """
    direct = defaultdict(list)
    person_backups = defaultdict(list)
    for rel in relationships:
        if rel["kind"] == "backup_for":
            target = nodes.get(rel["target_id"])
            if target is not None and target.get("kind") == "person":
                person_backups[rel["target_id"]].append(rel["source_id"])
            else:
                direct[rel["target_id"]].append(rel["source_id"])
    holders = defaultdict(set)
    for rel in relationships:
        if rel["kind"] in AUTHORITY_KINDS:
            holders[rel["target_id"]].add(rel["source_id"])
    covered = {}
    uncovered = {}
    for entity_id in affected:
        entity = nodes[entity_id]
        required = set((entity.get("attributes") or {}).get("required_skills") or [])
        candidates = list(direct.get(entity_id, []))
        for holder in holders.get(entity_id, set()):
            if holder in disrupted_ids:
                candidates.extend(person_backups.get(holder, []))
        seen = set()
        unique_candidates = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique_candidates.append(candidate)
        good = []
        reason = None
        for person_id in unique_candidates:
            if person_id in disrupted_ids:
                reason = "backup_absent"
                continue
            person = nodes.get(person_id)
            if person is None or person.get("kind") != "person":
                continue
            skills = set((person.get("attributes") or {}).get("skills") or [])
            if required and not required.issubset(skills):
                reason = "backup_missing_skills"
                continue
            good.append(person_id)
        if good:
            covered[entity_id] = sorted(good)
        else:
            uncovered[entity_id] = reason or "no_backup_recorded"
    return covered, uncovered


def _money_at_risk(nodes, affected):
    """Per-currency exposure totals over affected money entities."""
    totals = {}
    counts = {"invoice_batch": 0, "payment": 0}
    for entity_id in affected:
        entity = nodes[entity_id]
        if entity.get("kind") not in MONEY_KINDS:
            continue
        attrs = entity.get("attributes") or {}
        amount = _as_decimal(attrs.get("amount", 0), where=f"entity {entity_id!r}")
        currency = attrs.get("currency") or "PKR"
        totals[currency] = totals.get(currency, Decimal("0")) + amount
        counts[entity["kind"]] += 1
    return {
        currency: float(total) for currency, total in sorted(totals.items())
    }, counts


def _has_approval_bottleneck(nodes, relationships, affected):
    approvers = defaultdict(set)
    pending = set()
    for entity_id, entity in nodes.items():
        if entity.get("kind") in MONEY_KINDS:
            status = (entity.get("attributes") or {}).get("status")
            if status in ("awaiting_approval", "pending", "open"):
                pending.add(entity_id)
    for rel in relationships:
        if rel["kind"] == "approves" and rel["target_id"] in pending:
            approvers[rel["target_id"]].add(rel["source_id"])
    for batch_id in pending:
        holders = approvers.get(batch_id, set())
        if holders and holders.issubset(set(affected)):
            return True
    return False


def _severity(nodes, affected, money, bottleneck):
    """critical / high / medium / low from exposure, critical processes,
    and approval bottleneck."""
    critical_processes = sum(
        1
        for entity_id in affected
        if nodes[entity_id].get("kind") == "process"
        and (nodes[entity_id].get("attributes") or {}).get("critical")
    )
    total_money = sum(money.values())
    if bottleneck or (critical_processes >= 2 and total_money > 0):
        return "critical"
    if critical_processes >= 1 or total_money > 0 or len(affected) >= 8:
        return "high"
    if len(affected) >= 3:
        return "medium"
    return "low"


def _business_impact(nodes, affected, money, counts, severity, bottleneck, event):
    """Plain-language impact sentences built only from snapshot facts."""
    names = sorted(
        nodes[entity_id].get("name") or entity_id for entity_id in affected
    )
    processes = sorted(
        nodes[entity_id].get("name") or entity_id
        for entity_id in affected
        if nodes[entity_id].get("kind") == "process"
    )
    kind_label = event["kind"].replace("_", " ")
    duration = event.get("duration_days", DEFAULT_HORIZON_DAYS)
    lines = [
        f"A {kind_label} affecting {len(event['target_ids'])} target(s) "
        f"for {duration} day(s) puts {len(affected)} connected item(s) at risk."
    ]
    if processes:
        lines.append(
            f"Affected processes: {', '.join(processes)}."
        )
    if money:
        parts = ", ".join(f"{total:,.2f} {currency}" for currency, total in money.items())
        lines.append(
            f"Money at risk: {parts} across "
            f"{counts['invoice_batch']} invoice batch(es) and "
            f"{counts['payment']} payment(s)."
        )
    if bottleneck:
        lines.append(
            "Approval bottleneck: at least one pending invoice batch or "
            "payment has no available approver while this disruption lasts."
        )
    if severity in ("critical", "high"):
        lines.append(
            "Severity is "
            f"{severity}: recovery should start before the next approval "
            "or payment window."
        )
    if not names:
        lines.append("No connected items were found in the snapshot.")
    return lines


def _recovery_options(nodes, relationships, affected, covered, uncovered, event):
    """Ranked practical recovery options. Advisory only — every option
    names what it covers, who owns it, an ETA, and whether an approval
    is required before it may be executed. Nothing is executed here."""
    options = []
    disrupted = set(event.get("_disrupted_ids", []))

    # 1. Recorded backup person (highest rank when the backup is usable).
    for entity_id in sorted(covered):
        for backup_id in covered[entity_id]:
            options.append(
                {
                    "rank": 1,
                    "kind": "use_backup",
                    "action": (
                        f"Ask {nodes[backup_id].get('name') or backup_id} "
                        f"to cover {nodes[entity_id].get('name') or entity_id}."
                    ),
                    "covers": [entity_id],
                    "owner": backup_id,
                    "eta_days": 1,
                    "requires_approval": True,
                    "note": (
                        "Recorded backup link only; the backup must hold "
                        "the required access before starting — verify, do "
                        "not assume."
                    ),
                }
            )

    # 2. Team reassignment: another member of the same team with skills.
    team_members = defaultdict(list)
    for rel in relationships:
        if rel["kind"] == "member_of":
            team_members[rel["target_id"]].append(rel["source_id"])
    for entity_id in sorted(uncovered):
        entity = nodes[entity_id]
        required = set((entity.get("attributes") or {}).get("required_skills") or [])
        for rel in relationships:
            if rel["kind"] not in AUTHORITY_KINDS or rel["target_id"] != entity_id:
                continue
            holder = nodes.get(rel["source_id"])
            if holder is None or holder.get("kind") != "person":
                continue
            for team_id, members in team_members.items():
                if rel["source_id"] in members:
                    for member in sorted(members):
                        if member in disrupted or member == rel["source_id"]:
                            continue
                        skills = set(
                            (nodes[member].get("attributes") or {}).get("skills") or []
                        )
                        if required and not required.issubset(skills):
                            continue
                        options.append(
                            {
                                "rank": 2,
                                "kind": "reassign_to_team_member",
                                "action": (
                                    f"Reassign "
                                    f"{nodes[entity_id].get('name') or entity_id} "
                                    f"to {nodes[member].get('name') or member} "
                                    f"(same team)."
                                ),
                                "covers": [entity_id],
                                "owner": member,
                                "eta_days": 1,
                                "requires_approval": True,
                                "note": "Temporary reassignment; revert when the holder returns.",
                            }
                        )
                        break
                    break

    # 3. Alternative supplier: another supplier feeding the same target.
    supplied_targets = defaultdict(set)
    for rel in relationships:
        if rel["kind"] == "supplies":
            supplied_targets[rel["target_id"]].add(rel["source_id"])
    if event["kind"] == "supplier_failure":
        for target_id in sorted(supplied_targets):
            if target_id not in affected:
                continue
            alternatives = sorted(
                supplier
                for supplier in supplied_targets[target_id]
                if supplier not in disrupted
                and nodes[supplier].get("kind") == "supplier"
            )
            for alt in alternatives:
                options.append(
                    {
                        "rank": 3,
                        "kind": "switch_supplier",
                        "action": (
                            f"Switch supply of "
                            f"{nodes[target_id].get('name') or target_id} to "
                            f"{nodes[alt].get('name') or alt}."
                        ),
                        "covers": [target_id],
                        "owner": None,
                        "eta_days": 3,
                        "requires_approval": True,
                        "note": (
                            "Supplier switch is sensitive: owner approval "
                            "required before any order moves."
                        ),
                    }
                )

    # 4. Manual workaround where the process records one.
    for entity_id in sorted(affected):
        entity = nodes[entity_id]
        if entity.get("kind") == "process" and (
            entity.get("attributes") or {}
        ).get("manual_workaround"):
            options.append(
                {
                    "rank": 4,
                    "kind": "manual_workaround",
                    "action": (
                        "Use the recorded manual workaround for "
                        f"{entity.get('name') or entity_id}: "
                        f"{(entity.get('attributes') or {}).get('manual_workaround')}."
                    ),
                    "covers": [entity_id],
                    "owner": None,
                    "eta_days": 1,
                    "requires_approval": False,
                    "note": "Workaround only; restore the normal path afterwards.",
                }
            )

    # 5. Escalate to the owner — always present as the last resort.
    options.append(
        {
            "rank": 5,
            "kind": "escalate",
            "action": "Escalate to the owner with the impact summary and ask for direction.",
            "covers": sorted(affected),
            "owner": None,
            "eta_days": 1,
            "requires_approval": False,
            "note": "Fallback when no backup, reassignment, alternative or workaround covers the impact.",
        }
    )

    # Deterministic order: rank, then action text.
    options.sort(key=lambda o: (o["rank"], o["action"]))
    for index, option in enumerate(options, start=1):
        option["rank"] = index
    return options


def score_disruption(snapshot, event, *, depth=DEFAULT_DEPTH):
    """Analyse one disruption event over a dependency snapshot.

    ``event``: ``{"kind": <disruption kind>, "target_ids": [...],
    "duration_days": N}``. Returns a deterministic dict with the affected
    entities, severity, money at risk, business-impact sentences, ranked
    recovery options, uncovered entities, and assumptions. Advisory only.
    """
    global DEFAULT_DEPTH
    if not isinstance(event, dict):
        raise ValueError("Event must be a dict.")
    kind = event.get("kind")
    if kind not in DISRUPTION_KINDS:
        raise ValueError(f"Unknown disruption kind: {kind!r}.")
    target_ids = event.get("target_ids")
    if not isinstance(target_ids, list) or not target_ids:
        raise ValueError("Event needs a non-empty 'target_ids' list.")
    duration_days = event.get("duration_days", DEFAULT_HORIZON_DAYS)
    if (
        isinstance(duration_days, bool)
        or not isinstance(duration_days, (int, float))
        or duration_days <= 0
    ):
        raise ValueError("duration_days must be a positive number.")

    nodes, relationships = _validate_snapshot(snapshot)
    disrupted = _expand_targets(nodes, relationships, target_ids, kind)
    disrupted_set = set(disrupted)
    affected, truncated = _propagate(nodes, relationships, disrupted, depth=depth)

    covered, uncovered = _backup_coverage(nodes, relationships, affected, disrupted_set)
    money, counts = _money_at_risk(nodes, affected)
    bottleneck = _has_approval_bottleneck(nodes, relationships, affected)
    severity = _severity(nodes, affected, money, bottleneck)
    event_view = {
        "kind": kind,
        "target_ids": sorted(target_ids),
        "duration_days": duration_days,
    }
    impact_lines = _business_impact(
        nodes, affected, money, counts, severity, bottleneck, event_view
    )
    event_with_disrupted = dict(event_view)
    event_with_disrupted["_disrupted_ids"] = disrupted
    options = _recovery_options(
        nodes, relationships, affected, covered, uncovered, event_with_disrupted
    )

    affected_list = [
        {
            "id": entity_id,
            "kind": nodes[entity_id].get("kind"),
            "name": nodes[entity_id].get("name") or entity_id,
            "distance": info["distance"],
            "confidence": info["confidence"],
            "path": info["path"],
            "covered_by": covered.get(entity_id, []),
        }
        for entity_id, info in sorted(
            affected.items(), key=lambda item: (item[1]["distance"], item[0])
        )
    ]
    return {
        "event": event_view,
        "disrupted_ids": sorted(disrupted),
        "severity": severity,
        "affected": affected_list,
        "affected_count": len(affected),
        "uncovered": {
            entity_id: {"reason": reason} for entity_id, reason in sorted(uncovered.items())
        },
        "money_at_risk": money,
        "money_counts": counts,
        "approval_bottleneck": bottleneck,
        "business_impact": impact_lines,
        "recovery_options": options,
        "truncated": truncated,
        "assumptions": list(ASSUMPTIONS),
        "as_of": snapshot.get("as_of"),
    }


def assess_risk(snapshot, node_id):
    """Proactive 0–100 risk score for one node, with named factors.

    Factors: blast radius (dependents), backup coverage, approval
    bottleneck (sole approver of pending money), supplier concentration
    (sole supplier of a process), staleness (last confirmed > 30 days),
    criticality. Higher is riskier.
    """
    nodes, relationships = _validate_snapshot(snapshot)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"Unknown node id: {node_id!r}.")
    attrs = node.get("attributes") or {}
    score = 0
    factors = []

    dependents = {
        rel["source_id"]
        for rel in relationships
        if rel["target_id"] == node_id and rel["kind"] in ("depends_on", "supplies")
    } | {
        rel["target_id"]
        for rel in relationships
        if rel["source_id"] == node_id and rel["kind"] in AUTHORITY_KINDS
    }
    if dependents:
        blast = min(30, 5 * len(dependents))
        score += blast
        factors.append(f"blast_radius:{len(dependents)}_dependents(+{blast})")

    backups = [
        rel["source_id"]
        for rel in relationships
        if rel["kind"] == "backup_for" and rel["target_id"] == node_id
    ]
    required = set(attrs.get("required_skills") or [])
    usable = 0
    for person_id in backups:
        person = nodes.get(person_id)
        if person is None or person.get("kind") != "person":
            continue
        skills = set((person.get("attributes") or {}).get("skills") or [])
        if not required or required.issubset(skills):
            usable += 1
    if node.get("kind") in ("person", "team", "system", "supplier", "data"):
        if not backups:
            score += 20
            factors.append("no_backup_recorded(+20)")
        elif usable == 0:
            score += 15
            factors.append("backup_unusable(+15)")

    if node.get("kind") == "person":
        pending = set()
        for entity_id, entity in nodes.items():
            if entity.get("kind") in MONEY_KINDS:
                status = (entity.get("attributes") or {}).get("status")
                if status in ("awaiting_approval", "pending", "open"):
                    pending.add(entity_id)
        sole = []
        for batch_id in pending:
            approvers = {
                rel["source_id"]
                for rel in relationships
                if rel["kind"] == "approves" and rel["target_id"] == batch_id
            }
            if approvers == {node_id}:
                sole.append(batch_id)
        if sole:
            score += 20
            factors.append(f"sole_approver_of_{len(sole)}_pending(+20)")

    if node.get("kind") == "supplier":
        sole_supply = []
        for rel in relationships:
            if rel["kind"] == "supplies" and rel["source_id"] == node_id:
                others = {
                    other["source_id"]
                    for other in relationships
                    if other["kind"] == "supplies"
                    and other["target_id"] == rel["target_id"]
                    and other["source_id"] != node_id
                }
                if not others:
                    sole_supply.append(rel["target_id"])
        if sole_supply:
            score += 15
            factors.append(f"sole_supplier_for_{len(sole_supply)}(+15)")

    last_confirmed = attrs.get("last_confirmed_days_ago")
    if isinstance(last_confirmed, (int, float)) and not isinstance(last_confirmed, bool):
        if last_confirmed > STALE_DAYS:
            score += 10
            factors.append(f"stale_data:{last_confirmed}d(+10)")

    if attrs.get("critical"):
        score += 10
        factors.append("marked_critical(+10)")

    return {
        "node_id": node_id,
        "kind": node.get("kind"),
        "name": node.get("name") or node_id,
        "risk_score": min(100, score),
        "factors": factors,
    }


def rank_risks(snapshot):
    """Every node scored by :func:`assess_risk`, highest first.

    Ties are broken by node id so the ranking is deterministic.
    """
    nodes, _ = _validate_snapshot(snapshot)
    ranked = [assess_risk(snapshot, node_id) for node_id in nodes]
    ranked.sort(key=lambda item: (-item["risk_score"], item["node_id"]))
    return ranked


def recovery_progress(plan):
    """Progress bookkeeping for a recovery plan.

    ``plan``: ``{"steps": [{"id": ..., "status": "pending" |
    "in_progress" | "done" | "blocked" | "skipped", ...}]}``.
    Returns percent complete, counts per status, blocked steps, and the
    next actionable step (first pending/in-progress step in order).
    Pure bookkeeping — it never executes a step.
    """
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a dict.")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Plan must have a 'steps' list.")
    valid = {"pending", "in_progress", "done", "blocked", "skipped"}
    counts = {status: 0 for status in sorted(valid)}
    blocked = []
    next_step = None
    for step in steps:
        if not isinstance(step, dict) or not step.get("id"):
            raise ValueError("Every step must be a dict with a non-empty 'id'.")
        status = step.get("status")
        if status not in valid:
            raise ValueError(f"Unknown step status: {status!r}.")
        counts[status] += 1
        if status == "blocked":
            blocked.append(step["id"])
        if next_step is None and status in ("pending", "in_progress"):
            next_step = step["id"]
    total = len(steps)
    done = counts["done"] + counts["skipped"]
    percent = round(100.0 * done / total, 1) if total else 100.0
    return {
        "total_steps": total,
        "counts": counts,
        "percent_complete": percent,
        "blocked_steps": blocked,
        "next_step": next_step,
        "complete": total > 0 and counts["pending"] == 0 and counts["in_progress"] == 0 and not blocked,
    }
