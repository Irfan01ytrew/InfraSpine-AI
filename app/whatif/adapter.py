"""Twin snapshot adapter (Step AL slice 2).

Builds the plain-dict snapshot that :mod:`app.whatif.simulator` runs over,
from already-authorized sources — without changing any of them.

Two ways in:

- :func:`adapt_snapshot` — normalize any twin-shaped snapshot (the shape
  ``app.digital_twin.snapshot.build_snapshot`` returns) into a snapshot
  the simulator accepts, validating it with the simulator's own rules.
  Pure function; the Step S twin builder's output passes through
  verbatim (entities, relationships, ``as_of``, ``fingerprint``,
  ``sources`` / ``stale_sources``).
- :func:`snapshot_from_projection` — build a simulation snapshot from an
  authorized company-graph projection (``app.company_graph.service.
  projection``), the same projection the twin builder consumes. This is
  the graph-only path the API uses until the full multi-source twin
  builder lands; continuity nodes, finance rows and workflow runs are
  added by the twin builder, not here.
- :func:`build_simulation_snapshot` — the full multi-source snapshot via
  the Step S twin builder (Agent 55, ``app.digital_twin.snapshot``). The
  import is lazy: that module does not exist on this branch yet, so
  importing it at module level would break ``app.whatif``. Until it
  lands, the function raises a clear ``RuntimeError`` naming the
  integration point instead of failing obscurely.

The adapter is a projection reader, not a second source of truth: it
writes nothing, creates no tables, runs no migration, grants nothing,
and never pays. A money entity (``invoice_batch`` / ``payment``) without
a numeric ``amount`` fails loudly instead of silently dropping money
from "money at risk" — the owner must add the amount or remove the
node.

The company-graph kind mapping (``employee`` -> ``person``,
``department`` -> ``team``) and the record mapping intentionally mirror
``app.digital_twin.snapshot._graph_entity`` / ``_graph_relationship``
so both builders agree on one snapshot id space; if the twin builder
ever changes its mapping, this module must change with it.
"""

from __future__ import annotations

import copy

from app.whatif.simulator import _validate_snapshot, money_at_risk

# Company-graph / continuity kinds that are people or teams in twin terms.
# Mirrors app.digital_twin.snapshot.KIND_MAP.
KIND_MAP = {"employee": "person", "department": "team"}

# Metadata keys a twin snapshot may carry; passed through unchanged.
_METADATA_KEYS = ("sources", "stale_sources", "fingerprint")


def _twin_kind(kind):
    return KIND_MAP.get(str(kind), str(kind))


def _str_id(value, *, where):
    if value is None:
        raise ValueError(f"Missing id at {where}.")
    text = str(value)
    if not text:
        raise ValueError(f"Missing id at {where}.")
    return text


def adapt_snapshot(twin_snapshot):
    """Normalize a twin-shaped snapshot for the what-if simulator.

    Validates with the simulator's own snapshot rules (duplicate ids,
    dangling edges, negative/non-numeric money amounts all raise
    ``ValueError``) and returns a deep copy, so mutating the result
    never mutates the caller's snapshot. Extra metadata (``as_of``,
    ``truncated``, ``sources``, ``stale_sources``, ``fingerprint``) is
    passed through unchanged.
    """
    if not isinstance(twin_snapshot, dict):
        raise ValueError("Snapshot must be a dict with 'entities' and 'relationships'.")
    entities = twin_snapshot.get("entities")
    relationships = twin_snapshot.get("relationships")
    _validate_snapshot({"entities": entities, "relationships": relationships})
    snapshot = {
        "entities": copy.deepcopy(entities),
        "relationships": copy.deepcopy(relationships),
    }
    if "as_of" in twin_snapshot:
        snapshot["as_of"] = twin_snapshot["as_of"]
    snapshot["truncated"] = bool(twin_snapshot.get("truncated", False))
    for key in _METADATA_KEYS:
        if key in twin_snapshot:
            snapshot[key] = copy.deepcopy(twin_snapshot[key])
    return snapshot


def _projection_entity(record):
    """One company-graph projection record -> snapshot entity (or None)."""
    if not isinstance(record, dict) or record.get("record_type") != "entity":
        return None
    attributes = dict(record.get("attributes") or {})
    attributes.setdefault("source", "company_graph")
    if record.get("external_key") is not None:
        attributes.setdefault("external_key", record.get("external_key"))
    if record.get("department_id"):
        attributes.setdefault("department_id", str(record["department_id"]))
    return {
        "id": _str_id(record.get("id"), where="company-graph entity"),
        "kind": _twin_kind(record.get("kind")),
        "name": str(record.get("name") or record.get("id")),
        "attributes": attributes,
        "confidence": record.get("confidence", 1.0),
    }


def _projection_relationship(record, entity_ids):
    """One projection relationship -> snapshot edge (or None if dangling)."""
    if not isinstance(record, dict) or record.get("record_type") != "relationship":
        return None
    source, target = str(record.get("source_id") or ""), str(record.get("target_id") or "")
    if source not in entity_ids or target not in entity_ids:
        # The projection only returns edges between visible entities,
        # but a caller-built input may not; never emit a dangling edge.
        return None
    return {
        "id": _str_id(record.get("id"), where="company-graph relationship"),
        "source_id": source,
        "target_id": target,
        "kind": str(record.get("kind")),
        "confidence": record.get("confidence", 1.0),
        "evidence": list(record.get("evidence") or []),
    }


def snapshot_from_projection(projection):
    """Build a simulation snapshot from an authorized graph projection.

    ``projection`` is the dict ``app.company_graph.service.projection``
    returns: ``entities`` / ``relationships`` are ``public_record``
    dicts the caller is authorized to see (hidden records are excluded
    by construction, so they can never leak into the snapshot), plus
    ``as_of`` / ``truncated`` / ``fingerprint``. Only the graph is
    represented here — continuity nodes, finance money flow and workflow
    freshness come from the twin builder (see
    :func:`build_simulation_snapshot`).
    """
    if not isinstance(projection, dict):
        raise ValueError("Projection must be a dict with 'entities' and 'relationships'.")
    entities, entity_ids = [], set()
    for record in projection.get("entities") or []:
        entity = _projection_entity(record)
        if entity is None:
            continue
        if entity["id"] in entity_ids:
            raise ValueError(f"Duplicate entity id in snapshot: {entity['id']!r}.")
        entity_ids.add(entity["id"])
        entities.append(entity)
    relationships = []
    for record in projection.get("relationships") or []:
        edge = _projection_relationship(record, entity_ids)
        if edge is not None:
            relationships.append(edge)
    # Deterministic order: simulation results must not depend on input
    # row order.
    entities.sort(key=lambda e: e["id"])
    relationships.sort(key=lambda e: (e["id"], e["source_id"], e["target_id"]))
    snapshot = {
        "entities": entities,
        "relationships": relationships,
        "as_of": projection.get("as_of"),
        "truncated": bool(projection.get("truncated", False)),
    }
    if "fingerprint" in projection:
        snapshot["fingerprint"] = projection["fingerprint"]
    _validate_snapshot(snapshot)
    return snapshot


def snapshot_summary(snapshot):
    """Counts + money at risk + freshness for one snapshot (API metadata).

    Validates the snapshot first, so a summary is never produced for a
    snapshot the simulator would reject.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict with 'entities' and 'relationships'.")
    _validate_snapshot({"entities": snapshot.get("entities"), "relationships": snapshot.get("relationships")})
    entities = snapshot["entities"]
    by_kind = {}
    for entity in entities:
        by_kind[entity["kind"]] = by_kind.get(entity["kind"], 0) + 1
    return {
        "entity_count": len(entities),
        "relationship_count": len(snapshot["relationships"]),
        "by_kind": by_kind,
        "money_at_risk": money_at_risk(entities),
        "stale_sources": list(snapshot.get("stale_sources") or []),
        "fingerprint": snapshot.get("fingerprint"),
        "as_of": snapshot.get("as_of"),
        "truncated": bool(snapshot.get("truncated", False)),
    }


async def build_simulation_snapshot(db, graph_scope, *, continuity_tenant_id=None,
                                    group_id=None, limit=500):
    """Full multi-source snapshot via the Step S twin builder.

    Delegates to ``app.digital_twin.snapshot.build_twin_snapshot``
    (Agent 55, Step S slice 2) and adapts its output. The import is
    lazy on purpose: that module does not exist on this branch yet, so
    importing it at module level would break ``app.whatif``. Until it
    lands, this raises a ``RuntimeError`` naming the integration point;
    callers that only need the graph should use
    :func:`snapshot_from_projection` instead.
    """
    try:
        from app.digital_twin.snapshot import build_twin_snapshot
    except ImportError as exc:
        raise RuntimeError(
            "Twin snapshot builder (Step S slice 2, app.digital_twin.snapshot) "
            "is not available on this branch yet; build the snapshot from an "
            "authorized company-graph projection with snapshot_from_projection instead."
        ) from exc
    twin = await build_twin_snapshot(
        db, graph_scope, continuity_tenant_id=continuity_tenant_id,
        group_id=group_id, limit=limit)
    return adapt_snapshot(twin)
