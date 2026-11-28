"""Twin drift detection (Step K v2 slice 1).

Pure functions over twin snapshots. No I/O, no database, no network,
no secrets, no imports from Step S modules (they do not exist on this
branch yet — this module is stdlib only, so it stands alone).

Step S can answer *whether* the twin changed (``horizon`` only
compares fingerprints, a boolean). Nothing anywhere answers *what*
changed or *how much*. That is what this module does: a deterministic
diff between two twin snapshots — added / removed / changed entities
and relationships (attribute-level), per-currency money deltas, a
0–100 drift score with documented weights, and a trust verdict
(``trustworthy`` / ``review`` / ``stale``). Every later v2 slice
(persisted versions, health score, drill reconciliation, twin-driven
proposals) builds on this diff.

Snapshot shape (same as ``app.digital_twin.snapshot.build_snapshot``):

- ``entities``: list of ``{"id", "kind", "name", "attributes"}``.
- ``relationships``: list of ``{"id", "source_id", "target_id",
  "kind", "confidence", "evidence"}``.
- Optional ``as_of`` (ISO timestamp), ``fingerprint`` (stored SHA-256),
  ``stale_sources`` (list of source names).

Safety: snapshot text is untrusted data. Names, attributes, and
evidence strings are compared as data only — never evaluated,
executed, or interpreted as instructions. Text can never grant a
permission, change a payee, or change an amount: amounts come only
from numeric ``amount`` attributes on money entities, and a
non-numeric or negative amount is a ``ValueError``, not a number.
The module has no approval, dispatch, or payment path at all.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

MONEY_KINDS = ("invoice_batch", "payment")

#: Hard caps so a hostile or broken snapshot cannot grind the VM.
MAX_ENTITIES = 50_000
MAX_RELATIONSHIPS = 100_000

#: Trust bands for :func:`diff_snapshots` (owner decision §6 of the
#: Step K plan doc — confirm or change the bands).
TRUSTWORTHY_MAX_SCORE = 25
REVIEW_MAX_SCORE = 60

#: Drift-score weights (documented, deterministic). Removed entities
#: weigh most — a vanished entity may mean lost coverage, not a
#: cleanup. Money movement is capped so a single big batch cannot
#: dominate the score on its own.
_REMOVED_ENTITY_POINTS = 10
_REMOVED_ENTITY_CAP = 40
_ADDED_ENTITY_POINTS = 2
_ADDED_ENTITY_CAP = 10
_CHANGED_ENTITY_POINTS = 3
_CHANGED_ENTITY_CAP = 15
_REMOVED_EDGE_POINTS = 3
_REMOVED_EDGE_CAP = 15
_ADDED_EDGE_POINTS = 1
_ADDED_EDGE_CAP = 5
_CHANGED_EDGE_POINTS = 2
_CHANGED_EDGE_CAP = 10
_MONEY_POINTS = 20

_TRUST_VALUES = ("trustworthy", "review", "stale")

__all__ = [
    "MONEY_KINDS",
    "MAX_ENTITIES",
    "MAX_RELATIONSHIPS",
    "REVIEW_MAX_SCORE",
    "TRUSTWORTHY_MAX_SCORE",
    "diff_snapshots",
    "drift_summary",
    "snapshot_fingerprint",
    "validate_snapshot",
]


def _canonical(value):
    """Canonical JSON for hashing/comparison (sorted keys)."""
    return json.dumps(value, sort_keys=True, default=str)


def snapshot_fingerprint(snapshot):
    """Fingerprint of a twin snapshot.

    Uses the fingerprint the snapshot builder stored when present;
    otherwise computes the same canonical hash (SHA-256 over the
    sorted JSON of entities + relationships) as
    ``app.digital_twin.horizon.snapshot_fingerprint``, so fingerprints
    agree once Step S merges.
    """
    stored = snapshot.get("fingerprint") if isinstance(snapshot, dict) else None
    if isinstance(stored, str) and stored:
        return stored
    canonical = _canonical(
        {
            "entities": snapshot.get("entities", []),
            "relationships": snapshot.get("relationships", []),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_decimal(value, *, where):
    """Money amounts: numeric (never bool/str) and >= 0, else ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Non-numeric money amount at {where}: {value!r}.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Non-numeric money amount at {where}: {value!r}.")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Invalid money amount at {where}: {value!r}.")
    return amount


def _check_jsonable(value, *, where):
    """Attributes/evidence must be plain JSON data, not exotic objects."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ValueError(f"Non-JSON value at {where}: {type(value).__name__}.")


def _validate_snapshot(snapshot, *, where):
    """Validate a snapshot with the same rules as the snapshot builder.

    Returns ``(entities_by_id, relationships_by_id)``. Raises
    ``ValueError`` on duplicate ids, dangling relationship endpoints,
    bad money amounts, non-string ids, or oversized snapshots.
    """
    if not isinstance(snapshot, dict):
        raise ValueError(f"Snapshot at {where} must be a dict.")
    entities = snapshot.get("entities", [])
    relationships = snapshot.get("relationships", [])
    if not isinstance(entities, list):
        raise ValueError(f"Snapshot at {where}: 'entities' must be a list.")
    if not isinstance(relationships, list):
        raise ValueError(f"Snapshot at {where}: 'relationships' must be a list.")
    stale_sources = snapshot.get("stale_sources", [])
    if not isinstance(stale_sources, list) or not all(
        isinstance(item, str) for item in stale_sources
    ):
        raise ValueError(
            f"Snapshot at {where}: 'stale_sources' must be a list of strings."
        )
    if len(entities) > MAX_ENTITIES:
        raise ValueError(
            f"Snapshot at {where}: {len(entities)} entities exceeds MAX_ENTITIES."
        )
    if len(relationships) > MAX_RELATIONSHIPS:
        raise ValueError(
            f"Snapshot at {where}: {len(relationships)} relationships exceeds "
            "MAX_RELATIONSHIPS."
        )

    by_id = {}
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError(f"Snapshot at {where}: entity must be a dict.")
        eid = entity.get("id")
        if not isinstance(eid, str) or not eid:
            raise ValueError(f"Snapshot at {where}: entity id must be a non-empty string.")
        if eid in by_id:
            raise ValueError(f"Duplicate entity id in snapshot at {where}: {eid!r}.")
        kind = entity.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"Snapshot at {where}: entity {eid!r} kind must be a non-empty string.")
        name = entity.get("name", "")
        if not isinstance(name, str):
            raise ValueError(f"Snapshot at {where}: entity {eid!r} name must be a string.")
        attributes = entity.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError(f"Snapshot at {where}: entity {eid!r} attributes must be a dict.")
        _check_jsonable(attributes, where=f"entity {eid!r} attributes")
        if kind in MONEY_KINDS:
            _as_decimal(attributes.get("amount"), where=f"entity {eid!r} amount")
        by_id[eid] = entity

    edges = {}
    for edge in relationships:
        if not isinstance(edge, dict):
            raise ValueError(f"Snapshot at {where}: relationship must be a dict.")
        rid = edge.get("id")
        if not isinstance(rid, str) or not rid:
            raise ValueError(
                f"Snapshot at {where}: relationship id must be a non-empty string."
            )
        if rid in edges:
            raise ValueError(f"Duplicate relationship id in snapshot at {where}: {rid!r}.")
        source = edge.get("source_id")
        target = edge.get("target_id")
        if not isinstance(source, str) or source not in by_id:
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} has dangling source {source!r}."
            )
        if not isinstance(target, str) or target not in by_id:
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} has dangling target {target!r}."
            )
        kind = edge.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} kind must be a non-empty string."
            )
        confidence = edge.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} confidence must be numeric."
            )
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} confidence out of range."
            )
        evidence = edge.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(
                f"Snapshot at {where}: relationship {rid!r} evidence must be a list."
            )
        _check_jsonable(evidence, where=f"relationship {rid!r} evidence")
        edges[rid] = edge
    return by_id, edges


def validate_snapshot(snapshot, *, where="snapshot"):
    """Validate one twin snapshot with the builder's rules.

    Public wrapper over :func:`_validate_snapshot` for callers that
    need to check a snapshot without diffing it — the version store
    (Step K slice 2) validates every snapshot it records, so a
    malformed snapshot raises ``ValueError`` and is never stored.
    Returns ``None`` on success.
    """
    _validate_snapshot(snapshot, where=where)
    return None


def _money_totals(entities_by_id):
    """Per-currency money totals, same convention as ``impact.money_at_risk``.

    Returns ``{"totals": {currency: float}, "batch_count": n,
    "payment_count": n}``. Amounts are exposure only.
    """
    totals = {}
    batch_count = payment_count = 0
    for entity in entities_by_id.values():
        kind = entity.get("kind")
        if kind not in MONEY_KINDS:
            continue
        attributes = entity.get("attributes", {}) or {}
        amount = _as_decimal(attributes.get("amount"), where=f"entity {entity.get('id')!r}")
        currency = str(attributes.get("currency") or "unspecified")
        totals[currency] = totals.get(currency, Decimal("0")) + amount
        if kind == "invoice_batch":
            batch_count += 1
        else:
            payment_count += 1
    return {
        "totals": {currency: float(total) for currency, total in sorted(totals.items())},
        "batch_count": batch_count,
        "payment_count": payment_count,
    }


def _short_value(value):
    """Bounded rendering of an attribute value for a diff entry.

    Scalars are shown as-is when short; nested values are recorded as
    ``"<changed>"`` so reports stay bounded and never leak large blobs.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        text = str(value)
        return value if len(text) <= 200 else "<changed>"
    return "<changed>"


def _attribute_changes(old_attrs, new_attrs):
    """Top-level attribute keys that differ, with bounded old/new values."""
    changes = []
    for key in sorted(set(old_attrs) | set(new_attrs)):
        old = old_attrs.get(key, None)
        new = new_attrs.get(key, None)
        if _canonical(old) != _canonical(new):
            changes.append(
                {"key": key, "old": _short_value(old), "new": _short_value(new)}
            )
    return changes


def _drift_score(*, removed_entities, added_entities, changed_entities,
                 removed_edges, added_edges, changed_edges, money_old, money_new):
    """0–100 drift score with documented weights (see module docstring)."""
    score = 0
    score += min(_REMOVED_ENTITY_CAP, len(removed_entities) * _REMOVED_ENTITY_POINTS)
    score += min(_ADDED_ENTITY_CAP, len(added_entities) * _ADDED_ENTITY_POINTS)
    score += min(_CHANGED_ENTITY_CAP, len(changed_entities) * _CHANGED_ENTITY_POINTS)
    score += min(_REMOVED_EDGE_CAP, len(removed_edges) * _REMOVED_EDGE_POINTS)
    score += min(_ADDED_EDGE_CAP, len(added_edges) * _ADDED_EDGE_POINTS)
    score += min(_CHANGED_EDGE_CAP, len(changed_edges) * _CHANGED_EDGE_POINTS)
    largest_share = 0.0
    for currency in sorted(set(money_old["totals"]) | set(money_new["totals"])):
        old_total = Decimal(str(money_old["totals"].get(currency, 0.0)))
        new_total = Decimal(str(money_new["totals"].get(currency, 0.0)))
        base = max(old_total, new_total)
        if base > 0:
            share = float(abs(new_total - old_total) / base)
            largest_share = max(largest_share, share)
    score += int(_MONEY_POINTS * largest_share)
    return min(100, score)


def diff_snapshots(old, new):
    """Deterministic diff between two twin snapshots.

    Returns a plain-dict drift report (see the module docstring for the
    shape). ``old`` and ``new`` are validated first — a malformed
    snapshot raises ``ValueError`` instead of producing a misleading
    diff. The report is deterministic: same pair of snapshots always
    produces the byte-identical report (ids sorted, currencies sorted).
    """
    old_entities, old_edges = _validate_snapshot(old, where="old snapshot")
    new_entities, new_edges = _validate_snapshot(new, where="new snapshot")

    old_ids, new_ids = set(old_entities), set(new_entities)
    added_entities = sorted(new_ids - old_ids)
    removed_entities = sorted(old_ids - new_ids)
    changed_entities = []
    for eid in sorted(old_ids & new_ids):
        before, after = old_entities[eid], new_entities[eid]
        attr_changes = _attribute_changes(
            before.get("attributes", {}) or {}, after.get("attributes", {}) or {}
        )
        kind_changed = before.get("kind") != after.get("kind")
        name_changed = before.get("name", "") != after.get("name", "")
        if attr_changes or kind_changed or name_changed:
            changed_entities.append(
                {
                    "id": eid,
                    "kind": after.get("kind"),
                    "name": after.get("name", ""),
                    "kind_changed": kind_changed,
                    "name_changed": name_changed,
                    "changed_attributes": attr_changes,
                }
            )

    old_edge_ids, new_edge_ids = set(old_edges), set(new_edges)
    added_edges = sorted(new_edge_ids - old_edge_ids)
    removed_edges = sorted(old_edge_ids - new_edge_ids)
    changed_edges = []
    for rid in sorted(old_edge_ids & new_edge_ids):
        before, after = old_edges[rid], new_edges[rid]
        kind_changed = before.get("kind") != after.get("kind")
        confidence_changed = float(before.get("confidence", 1.0)) != float(
            after.get("confidence", 1.0)
        )
        if kind_changed or confidence_changed:
            changed_edges.append(
                {
                    "id": rid,
                    "kind_old": before.get("kind"),
                    "kind_new": after.get("kind"),
                    "confidence_old": float(before.get("confidence", 1.0)),
                    "confidence_new": float(after.get("confidence", 1.0)),
                }
            )

    money_old = _money_totals(old_entities)
    money_new = _money_totals(new_entities)
    money_delta = {
        "totals": {
            currency: float(
                Decimal(str(money_new["totals"].get(currency, 0.0)))
                - Decimal(str(money_old["totals"].get(currency, 0.0)))
            )
            for currency in sorted(set(money_old["totals"]) | set(money_new["totals"]))
        },
        "batch_count": money_new["batch_count"] - money_old["batch_count"],
        "payment_count": money_new["payment_count"] - money_old["payment_count"],
    }

    score = _drift_score(
        removed_entities=removed_entities,
        added_entities=added_entities,
        changed_entities=changed_entities,
        removed_edges=removed_edges,
        added_edges=added_edges,
        changed_edges=changed_edges,
        money_old=money_old,
        money_new=money_new,
    )

    stale_sources = new.get("stale_sources") or []
    if score > REVIEW_MAX_SCORE or stale_sources:
        trust = "stale"
    elif score > TRUSTWORTHY_MAX_SCORE:
        trust = "review"
    else:
        trust = "trustworthy"

    fingerprint_old = snapshot_fingerprint(old)
    fingerprint_new = snapshot_fingerprint(new)
    return {
        "fingerprint_old": fingerprint_old,
        "fingerprint_new": fingerprint_new,
        "as_of_old": old.get("as_of"),
        "as_of_new": new.get("as_of"),
        "entities": {
            "added": added_entities,
            "removed": removed_entities,
            "changed": changed_entities,
        },
        "relationships": {
            "added": added_edges,
            "removed": removed_edges,
            "changed": changed_edges,
        },
        "money": {"old": money_old, "new": money_new, "delta": money_delta},
        "counts": {
            "entities_old": len(old_entities),
            "entities_new": len(new_entities),
            "relationships_old": len(old_edges),
            "relationships_new": len(new_edges),
        },
        "drift_score": score,
        "trust": trust,
        "stale_sources": sorted(stale_sources),
        "changed": fingerprint_old != fingerprint_new,
    }


def drift_summary(report):
    """One deterministic summary line for a drift report.

    Counts and totals only — no attribute values, no names, no secrets.
    """
    entities = report["entities"]
    relationships = report["relationships"]
    delta_totals = {
        currency: total
        for currency, total in sorted(report["money"]["delta"]["totals"].items())
        if total != 0
    }
    if delta_totals:
        money_part = ", ".join(
            f"{currency} {total:+,.2f}" for currency, total in delta_totals.items()
        )
    else:
        money_part = "none"
    stale = len(report.get("stale_sources") or [])
    return (
        f"Twin drift {report['drift_score']}/100 ({report['trust']}): "
        f"+{len(entities['added'])} -{len(entities['removed'])} "
        f"~{len(entities['changed'])} entities, "
        f"+{len(relationships['added'])} -{len(relationships['removed'])} "
        f"~{len(relationships['changed'])} relationships; "
        f"money delta {money_part}; {stale} stale source(s)."
    )
