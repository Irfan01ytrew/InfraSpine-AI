"""Cross-source conflict reporting (Step K v2 slice 7).

Pure functions over per-source entity views. A conflict is two (or
more) sources disagreeing about the SAME entity field — kind, name,
or one attribute key. Both sides are listed; nothing is silently
resolved by "graph wins" alone: the merged snapshot's choice is a
display decision, the disagreement stays visible here until a human
resolves it.

An entity present in one source and absent in another is NOT a
conflict on its own — sources cover different subsets of the twin.
Only differing values for the same entity + field conflict.

Money rules follow ``app.digital_twin.drift``: amounts are validated
with the same precision/scale, and every value is bounded to 200
characters so one hostile attribute cannot bloat the report.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

MAX_CONFLICTS = 500
_MAX_VALUE_CHARS = 200
_MONEY_KINDS = ("invoice_batch", "payment")
_FIELD_KEYS = ("kind", "name")


def _as_decimal(value, *, what: str) -> Decimal:
    """Mirror ``drift._as_decimal``: ints/floats/strs only, exact."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{what} amount must be a number or numeric string, got {type(value).__name__}")
    try:
        amount = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{what} amount is not a valid decimal: {value!r}") from None
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError(f"{what} amount is not a finite decimal with at most 2 decimals: {value!r}")
    return amount


def _validate_money(entity: dict, *, source: str) -> None:
    kind = entity.get("kind")
    if kind in _MONEY_KINDS:
        amount = entity.get("attributes", {}).get("amount")
        if amount is not None:
            _as_decimal(amount, what=f"{source} {kind} {entity.get('id')!r}")


def _short_value(value) -> str:
    text = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return text[:_MAX_VALUE_CHARS]


def _validate_entity(entity: dict, *, source: str) -> None:
    if not isinstance(entity, dict):
        raise ValueError(f"{source} entities must be dicts, got {type(entity).__name__}")
    entity_id = entity.get("id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError(f"{source} entities need a non-empty string 'id', got {entity!r}")
    kind = entity.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"{source} entity {entity_id!r} needs a non-empty string 'kind'")
    name = entity.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError(f"{source} entity {entity_id!r} 'name' must be a string")
    attributes = entity.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError(f"{source} entity {entity_id!r} 'attributes' must be a dict")
    for key, value in attributes.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{source} entity {entity_id!r} attribute keys must be non-empty strings")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError(f"{source} entity {entity_id!r} attribute {key!r} must be a JSON scalar")
    _validate_money(entity, source=source)


def _conflict_key(value, *, field: str, kind: str) -> str:
    """Comparable key for one field value (money amounts by value).

    Amounts are canonicalised to exactly two decimals, so ``"100.00"``,
    ``100`` and ``100.0`` are the same amount and not a conflict.
    """
    if field == "attributes.amount" and kind in _MONEY_KINDS and value is not None:
        return str(_as_decimal(value, what="conflict amount").quantize(Decimal("0.01")))
    return json.dumps(value, sort_keys=True, default=str)


def detect_conflicts(source_views: list, *, max_conflicts: int = MAX_CONFLICTS) -> dict:
    """Detect cross-source disagreements about the same entity field.

    ``source_views`` is a list of ``{"source": str, "entities": [...]}``
    where each entity is ``{"id", "kind", "name", "attributes"}``.
    Returns ``{"conflicts", "total", "truncated", "sources",
    "entities_compared"}``. Each conflict is ``{"entity_id", "field",
    "kinds", "values"}``: ``kinds`` maps source -> the entity's kind
    there, ``values`` maps source -> the bounded value there.
    """
    if not isinstance(source_views, list):
        raise ValueError(f"source_views must be a list, got {type(source_views).__name__}")
    if not isinstance(max_conflicts, int) or isinstance(max_conflicts, bool) or max_conflicts < 1:
        raise ValueError(f"max_conflicts must be a positive int, got {max_conflicts!r}")
    by_entity: dict[str, dict[str, dict]] = {}
    sources = []
    for view in source_views:
        if not isinstance(view, dict):
            raise ValueError(f"source views must be dicts, got {view!r}")
        name = view.get("source")
        if not isinstance(name, str) or not name:
            raise ValueError(f"source views need a non-empty string 'source', got {view!r}")
        if name in sources:
            raise ValueError(f"duplicate source view {name!r}")
        sources.append(name)
        entities = view.get("entities")
        if not isinstance(entities, list):
            raise ValueError(f"source {name!r} 'entities' must be a list")
        seen: set[str] = set()
        for entity in entities:
            _validate_entity(entity, source=name)
            if entity["id"] in seen:
                raise ValueError(f"source {name!r} lists entity {entity['id']!r} twice")
            seen.add(entity["id"])
            by_entity.setdefault(entity["id"], {})[name] = entity

    conflicts = []
    for entity_id in sorted(by_entity):
        views = by_entity[entity_id]
        if len(views) < 2:
            continue
        kinds = {source: entity["kind"] for source, entity in views.items()}
        fields: dict[str, dict[str, object]] = {}
        for field in _FIELD_KEYS:
            values = {source: entity.get(field) for source, entity in views.items()}
            fields[field] = values
        attribute_keys = sorted({key for entity in views.values() for key in entity.get("attributes", {})})
        for key in attribute_keys:
            field = f"attributes.{key}"
            fields[field] = {source: entity.get("attributes", {}).get(key) for source, entity in views.items()}
        for field, values in fields.items():
            distinct = set()
            for source, value in values.items():
                entity_kind = kinds[source]
                distinct.add(_conflict_key(value, field=field, kind=entity_kind))
            if len(distinct) > 1:
                conflicts.append({
                    "entity_id": entity_id,
                    "field": field,
                    "kinds": dict(sorted(kinds.items())),
                    "values": {source: _short_value(value) for source, value in sorted(values.items())},
                })
    conflicts.sort(key=lambda c: (c["entity_id"], c["field"]))
    total = len(conflicts)
    return {
        "conflicts": conflicts[:max_conflicts],
        "total": total,
        "truncated": total > max_conflicts,
        "sources": sorted(sources),
        "entities_compared": len(by_entity),
    }


def conflict_summary(result: dict) -> str:
    """One-line, count-only summary of a ``detect_conflicts`` result."""
    total = result.get("total", 0)
    entities = result.get("entities_compared", 0)
    line = f"{total} conflict(s) across {entities} compared entit{'y' if entities == 1 else 'ies'}"
    if result.get("truncated"):
        line += " (truncated)"
    return line


def redact_conflicts_for_viewer(result: dict, *, is_owner: bool) -> dict:
    """Money-safe view of a conflict report.

    Owners see both sides in full. Non-owners see the disagreement
    (entity, field, that values differ) but any value belonging to a
    money-kind entity is replaced with ``"<redacted>"`` and the
    report carries ``money_redacted: true``.
    """
    if is_owner:
        return result
    redacted = dict(result)
    redacted["conflicts"] = []
    money_hidden = False
    for conflict in result.get("conflicts", []):
        kinds = conflict.get("kinds", {})
        entry = dict(conflict)
        if any(kind in _MONEY_KINDS for kind in kinds.values()):
            entry["values"] = {source: "<redacted>" for source in conflict.get("values", {})}
            money_hidden = True
        redacted["conflicts"].append(entry)
    redacted["money_redacted"] = money_hidden
    return redacted
