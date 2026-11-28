"""Company Resilience AI — read-only snapshot builder (slice 3).

Pure adapters that turn the rows the service layer reads (through
``app/platform`` scopes — this module never touches the database and never
grants access) into the plain-dict dependency snapshot that
:mod:`app.resilience.impact` and :mod:`app.resilience.watch` consume.

The two company graphs are still unmerged — the merge is Step G's job —
so this builder reads **both graphs as they exist today** and produces one
snapshot:

- ``company_graph_records`` (entities + relationships in one table) and
  ``bc_nodes`` / ``bc_links`` are converted with source-prefixed ids
  (``cg:<uuid>`` / ``bc:<uuid>``) so the two graphs can never collide.
  The original key is kept in ``attributes["external_key"]`` so Step G's
  merge can match the same real-world entity across graphs; when the same
  external key appears in both graphs with a different kind or name, the
  company-graph version wins and the clash is listed in ``conflicts``.
- Finance rows (``fin_invoice_batches``, ``fin_payments``, ``fin_invoices``)
  become ``invoice_batch`` / ``payment`` money entities. A payment
  ``depends_on`` its batch. **Payee account numbers are never copied
  into a snapshot** (they are not needed for impact math and must not
  leak into alerts or logs).
- Approval edges (``approves``) are built from approval rows. The finance
  tables only know the approver's user id, so approvers not present in
  either graph become clearly-marked stub person entities keyed by that
  user id (``attributes["stub"] == True``), so the service layer or the
  Step G merge can replace them with the real graph person later.

Rules enforced here:

- Read-only: nothing is written anywhere; this module takes plain dicts
  and returns plain dicts.
- Fail fast on malformed rows (missing id/kind, bad confidence, negative
  or non-numeric money) — a broken snapshot must never silently feed the
  watch. Rows that are valid but unusable (retired, expired, dangling
  endpoint) are skipped and listed in ``skipped`` instead of crashing.
- Vendor → supplier edges are NOT built: finance vendor ids have no
  mapping to graph supplier entities yet. That mapping is an owner
  decision for the service layer; the gap is recorded in ``notes``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.resilience.impact import rank_risks

# Source prefixes — keep the two graphs from ever colliding in one snapshot.
CG_PREFIX = "cg"
BC_PREFIX = "bc"
FIN_PREFIX = "fin"

_KIND_ALIASES = {
    "employee": "person",
    "user": "person",
    "server": "system",
    "software": "system",
    "vendor": "supplier",
    "department": "team",
}

_MONEY_STATUSES_COUNTED = ("pending", "verified", "exception", "approved", "paid")


def _normalise_kind(kind):
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("Entity kind must be a non-empty string.")
    return _KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())


def _as_amount(value, *, where):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.") from None
    if amount < 0:
        raise ValueError(f"Amount at {where} must not be negative, got {value!r}.")
    return float(amount)


def _confidence(value, *, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Confidence at {where} must be a number.")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"Confidence at {where} must be between 0 and 1.")
    return float(value)


def validate_snapshot(snapshot) -> list[str]:
    """Return a list of problems with a snapshot (empty means valid).

    Mirrors the structural checks in ``app.resilience.impact`` plus the
    money-amount check, as a problem list instead of raising.
    """
    problems: list[str] = []
    if not isinstance(snapshot, dict):
        return ["Snapshot must be a dict."]
    entities = snapshot.get("entities")
    relationships = snapshot.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        return ["Snapshot must have 'entities' and 'relationships' lists."]
    ids: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("id"):
            problems.append("Every entity must be a dict with a non-empty 'id'.")
            continue
        if entity["id"] in ids:
            problems.append(f"Duplicate entity id: {entity['id']!r}.")
        ids.add(entity["id"])
        attrs = entity.get("attributes") or {}
        if entity.get("kind") in ("invoice_batch", "payment"):
            amount = attrs.get("amount")
            if amount is not None:
                try:
                    _as_amount(amount, where=f"entity {entity['id']!r}")
                except ValueError as exc:
                    problems.append(str(exc))
    for rel in relationships:
        if not isinstance(rel, dict):
            problems.append("Every relationship must be a dict.")
            continue
        for key in ("source_id", "target_id", "kind"):
            if not rel.get(key):
                problems.append(f"Every relationship needs a non-empty {key!r}.")
        if rel.get("source_id") not in ids or rel.get("target_id") not in ids:
            problems.append(
                "Relationship references unknown entity: "
                f"{rel.get('source_id')!r} -> {rel.get('target_id')!r}."
            )
        try:
            _confidence(rel.get("confidence", 1.0), where="relationship")
        except ValueError as exc:
            problems.append(str(exc))
    return problems


def from_company_graph(records) -> dict:
    """Convert ``company_graph_records`` rows to snapshot entities/edges.

    Each row is a plain dict with the GraphRecord columns: ``id``,
    ``record_type`` (``entity`` | ``relationship``), ``external_key``,
    ``kind``, ``name``, ``source_id`` / ``target_id`` (relationships),
    ``visibility``, ``attributes``, ``confidence``, ``retired``,
    ``valid_from`` / ``valid_to``. The caller passes live rows: retired
    rows and relationships whose endpoints are absent from this batch
    are skipped and listed in ``skipped``.
    """
    if not isinstance(records, list):
        raise ValueError("company_graph records must be a list.")
    entities: list[dict] = []
    relationships: list[dict] = []
    skipped: list[dict] = []
    entity_ids: set[str] = set()
    for row in records:
        if not isinstance(row, dict) or not row.get("id"):
            raise ValueError("Every company-graph record needs a non-empty 'id'.")
        record_type = row.get("record_type")
        if record_type not in ("entity", "relationship"):
            skipped.append({"record_id": str(row["id"]), "reason": "unknown_record_type"})
            continue
        if row.get("retired"):
            skipped.append({"record_id": str(row["id"]), "reason": "retired"})
            continue
        row_id = f"{CG_PREFIX}:{row['id']}"
        if record_type == "entity":
            kind = _normalise_kind(row.get("kind"))
            _confidence(row.get("confidence", 1.0), where=f"record {row['id']!r}")
            attrs = dict(row.get("attributes") or {})
            attrs.setdefault("external_key", row.get("external_key"))
            attrs.setdefault("visibility", row.get("visibility") or "restricted")
            attrs["source_graph"] = "company_graph"
            entities.append(
                {
                    "id": row_id,
                    "kind": kind,
                    "name": row.get("name") or row_id,
                    "attributes": attrs,
                }
            )
            entity_ids.add(row_id)
        else:
            source = f"{CG_PREFIX}:{row.get('source_id')}" if row.get("source_id") else None
            target = f"{CG_PREFIX}:{row.get('target_id')}" if row.get("target_id") else None
            relationships.append(
                {
                    "id": row_id,
                    "source_id": source,
                    "target_id": target,
                    "kind": (row.get("kind") or "").strip().lower(),
                    "confidence": _confidence(row.get("confidence", 1.0), where=f"record {row['id']!r}"),
                }
            )
    # Second pass: drop relationships whose endpoints are not in this batch.
    kept: list[dict] = []
    for rel in relationships:
        if rel["source_id"] in entity_ids and rel["target_id"] in entity_ids:
            kept.append(rel)
        else:
            skipped.append({"record_id": rel["id"], "reason": "dangling_endpoint"})
    return {"entities": entities, "relationships": kept, "skipped": skipped}


def from_continuity(nodes, links) -> dict:
    """Convert ``bc_nodes`` / ``bc_links`` rows to snapshot entities/edges.

    Nodes are dicts with ``id``, ``external_key``, ``kind``, ``name``,
    ``data`` (JSONB attributes), ``user_id`` / ``department_id``. Links
    are dicts with ``source_id``, ``target_id``, ``kind``, ``data``.
    Links whose endpoints are absent are skipped and listed.
    """
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("continuity nodes and links must be lists.")
    entities: list[dict] = []
    skipped: list[dict] = []
    entity_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            raise ValueError("Every continuity node needs a non-empty 'id'.")
        node_id = f"{BC_PREFIX}:{node['id']}"
        attrs = dict(node.get("data") or {})
        attrs.setdefault("external_key", node.get("external_key"))
        if node.get("user_id") is not None:
            attrs.setdefault("user_id", str(node["user_id"]))
        if node.get("department_id") is not None:
            attrs.setdefault("department_id", str(node["department_id"]))
        attrs["source_graph"] = "continuity"
        entities.append(
            {
                "id": node_id,
                "kind": _normalise_kind(node.get("kind")),
                "name": node.get("name") or node_id,
                "attributes": attrs,
            }
        )
        entity_ids.add(node_id)
    relationships: list[dict] = []
    for link in links:
        if not isinstance(link, dict) or not link.get("source_id") or not link.get("target_id"):
            raise ValueError("Every continuity link needs source_id and target_id.")
        rel = {
            "id": f"{BC_PREFIX}:{link.get('id') or link['source_id'] + '->' + link['target_id'] + ':' + str(link.get('kind'))}",
            "source_id": f"{BC_PREFIX}:{link['source_id']}",
            "target_id": f"{BC_PREFIX}:{link['target_id']}",
            "kind": (link.get("kind") or "").strip().lower(),
            "confidence": _confidence(
                (link.get("data") or {}).get("confidence", 1.0),
                where=f"link {link.get('source_id')!r}->{link.get('target_id')!r}",
            ),
        }
        if rel["source_id"] in entity_ids and rel["target_id"] in entity_ids:
            relationships.append(rel)
        else:
            skipped.append({"record_id": rel["id"], "reason": "dangling_endpoint"})
    return {"entities": entities, "relationships": relationships, "skipped": skipped}


def from_finance(batches, payments, invoices=None, approvals=None) -> dict:
    """Convert finance rows to money entities and approval edges.

    ``batches``: ``{id, name, status}`` (``fin_invoice_batches``).
    ``payments``: ``{id, batch_id, vendor_id, amount, currency, status}``
    (``fin_payments``) — ``payee_account`` is deliberately never copied.
    ``invoices`` (optional): ``{batch_id, amount, currency, status}``
    (``fin_invoices``) — summed per batch over non-rejected statuses;
    a batch spanning several currencies gets no single amount, only the
    ``currencies`` list. ``approvals`` (optional): ``{target_type:
    "batch" | "payment", target_id, approver_id}`` — each becomes an
    ``approves`` edge from a stub person entity keyed by the approver's
    user id (``attributes["stub"] == True``) unless that approver is
    already represented by an entity in this finance input.
    """
    if not isinstance(batches, list) or not isinstance(payments, list):
        raise ValueError("finance batches and payments must be lists.")
    invoices = invoices or []
    approvals = approvals or []
    if not isinstance(invoices, list) or not isinstance(approvals, list):
        raise ValueError("finance invoices and approvals must be lists.")

    totals: dict[str, dict[str, float]] = {}
    currencies: dict[str, set[str]] = {}
    for invoice in invoices:
        if not isinstance(invoice, dict) or not invoice.get("batch_id"):
            raise ValueError("Every invoice needs a batch_id.")
        if (invoice.get("status") or "") not in _MONEY_STATUSES_COUNTED:
            continue
        batch_key = str(invoice["batch_id"])
        amount = _as_amount(invoice.get("amount", 0), where=f"invoice for batch {batch_key!r}")
        currency = invoice.get("currency") or "PKR"
        totals.setdefault(batch_key, {})[currency] = (
            totals.setdefault(batch_key, {}).get(currency, 0.0) + amount
        )
        currencies.setdefault(batch_key, set()).add(currency)

    entities: list[dict] = []
    skipped: list[dict] = []
    batch_ids: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict) or not batch.get("id"):
            raise ValueError("Every finance batch needs a non-empty 'id'.")
        entity_id = f"{FIN_PREFIX}:batch:{batch['id']}"
        batch_ids.add(entity_id)
        attrs: dict = {
            "status": batch.get("status"),
            "source_graph": "finance",
        }
        batch_totals = totals.get(str(batch["id"]), {})
        if len(batch_totals) == 1:
            currency, amount = next(iter(batch_totals.items()))
            attrs["amount"] = amount
            attrs["currency"] = currency
        elif len(batch_totals) > 1:
            attrs["currencies"] = sorted(batch_totals)
            attrs["amount"] = None
        entities.append(
            {"id": entity_id, "kind": "invoice_batch", "name": batch.get("name") or entity_id, "attributes": attrs}
        )

    relationships: list[dict] = []
    payment_ids: set[str] = set()
    for payment in payments:
        if not isinstance(payment, dict) or not payment.get("id"):
            raise ValueError("Every finance payment needs a non-empty 'id'.")
        entity_id = f"{FIN_PREFIX}:payment:{payment['id']}"
        payment_ids.add(entity_id)
        entities.append(
            {
                "id": entity_id,
                "kind": "payment",
                "name": payment.get("name") or entity_id,
                "attributes": {
                    "amount": _as_amount(payment.get("amount"), where=f"payment {payment['id']!r}"),
                    "currency": payment.get("currency") or "PKR",
                    "status": payment.get("status"),
                    "source_graph": "finance",
                },
            }
        )
        batch_entity = f"{FIN_PREFIX}:batch:{payment.get('batch_id')}"
        if batch_entity in batch_ids:
            relationships.append(
                {
                    "id": f"{entity_id}->depends_on->{batch_entity}",
                    "source_id": entity_id,
                    "target_id": batch_entity,
                    "kind": "depends_on",
                    "confidence": 1.0,
                }
            )
        else:
            skipped.append({"record_id": entity_id, "reason": "batch_not_in_snapshot"})

    known_persons: set[str] = set()
    for approval in approvals:
        if not isinstance(approval, dict):
            raise ValueError("Every approval needs to be a dict.")
        target_type = approval.get("target_type")
        if target_type not in ("batch", "payment"):
            raise ValueError(f"Approval target_type must be 'batch' or 'payment', got {target_type!r}.")
        target_id = f"{FIN_PREFIX}:{target_type}:{approval.get('target_id')}"
        if target_id not in batch_ids and target_id not in payment_ids:
            skipped.append({"record_id": target_id, "reason": "approval_target_not_in_snapshot"})
            continue
        approver = approval.get("approver_id")
        if not approver:
            raise ValueError("Every approval needs an approver_id.")
        person_id = f"{FIN_PREFIX}:person:{approver}"
        if person_id not in known_persons:
            known_persons.add(person_id)
            entities.append(
                {
                    "id": person_id,
                    "kind": "person",
                    "name": person_id,
                    "attributes": {
                        "stub": True,
                        "user_id": str(approver),
                        "source_graph": "finance-approval",
                    },
                }
            )
        relationships.append(
            {
                "id": f"{person_id}->approves->{target_id}",
                "source_id": person_id,
                "target_id": target_id,
                "kind": "approves",
                "confidence": 1.0,
            }
        )
    return {"entities": entities, "relationships": relationships, "skipped": skipped}


def build_snapshot(
    *,
    company_graph=None,
    continuity=None,
    finance=None,
    as_of=None,
    revision=None,
) -> dict:
    """Combine the source adapters into one validated snapshot.

    ``company_graph`` is the raw ``company_graph_records`` list (or None),
    ``continuity`` is a ``(nodes, links)`` tuple (or None), ``finance``
    is a ``(batches, payments, invoices, approvals)`` tuple (or None —
    the last two items may be omitted). Relationships are deduped by
    (source, target, kind), first wins. When the same ``external_key``
    appears in both graphs with a different kind or name, the
    company-graph version wins and the clash is listed in ``conflicts``
    for Step G's merge. Raises :class:`ValueError` if the combined
    snapshot fails :func:`validate_snapshot` — a broken snapshot must
    never feed the watch.
    """
    entities: list[dict] = []
    relationships: list[dict] = []
    skipped: list[dict] = []
    sources = {"company_graph": 0, "continuity": 0, "finance": 0}
    notes: list[str] = []
    by_external_key: dict[str, dict] = {}
    conflicts: list[dict] = []

    if company_graph is not None:
        part = from_company_graph(company_graph)
        for entity in part["entities"]:
            _register_external_key(entity, by_external_key, conflicts, source="company_graph")
        entities.extend(part["entities"])
        relationships.extend(part["relationships"])
        skipped.extend(part["skipped"])
        sources["company_graph"] = len(part["entities"])
    if continuity is not None:
        nodes, links = continuity
        part = from_continuity(nodes, links)
        for entity in part["entities"]:
            key = (entity.get("attributes") or {}).get("external_key")
            previous = by_external_key.get(key) if key else None
            if previous is not None and (
                previous["kind"] != entity["kind"] or previous["name"] != entity["name"]
            ):
                # Same real-world entity in both graphs, disagreeing:
                # the company-graph version wins (Step G merges them
                # properly); the continuity copy is dropped, not kept
                # alongside it, or the map would double-count one node.
                _register_external_key(entity, by_external_key, conflicts, source="continuity")
                skipped.append(
                    {"record_id": entity["id"], "reason": "external_key_conflict"}
                )
                continue
            _register_external_key(entity, by_external_key, conflicts, source="continuity")
            entities.append(entity)
        relationships.extend(part["relationships"])
        skipped.extend(part["skipped"])
        sources["continuity"] = len(part["entities"])
    if finance is not None:
        batches, payments = finance[0], finance[1]
        invoices = finance[2] if len(finance) > 2 else None
        approvals = finance[3] if len(finance) > 3 else None
        part = from_finance(batches, payments, invoices, approvals)
        entities.extend(part["entities"])
        relationships.extend(part["relationships"])
        skipped.extend(part["skipped"])
        sources["finance"] = len(part["entities"])
        notes.append(
            "Vendor-to-supplier edges are not built: finance vendor ids "
            "have no mapping to graph supplier entities yet (owner decision)."
        )
        notes.append(
            "Finance approvers appear as stub person entities keyed by "
            "user id; replace them with the graph person when known."
        )

    # Drop any edge whose endpoint did not survive the combine (e.g. it
    # pointed at a conflict loser) — a dangling edge must never reach
    # the watch.
    entity_ids = {entity["id"] for entity in entities}
    kept_edges: list[dict] = []
    for rel in relationships:
        if rel["source_id"] in entity_ids and rel["target_id"] in entity_ids:
            kept_edges.append(rel)
        else:
            skipped.append({"record_id": rel["id"], "reason": "dangling_endpoint"})
    relationships = kept_edges

    seen_edges: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for rel in relationships:
        key = (rel["source_id"], rel["target_id"], rel["kind"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        deduped.append(rel)

    snapshot = {
        "entities": entities,
        "relationships": deduped,
        "as_of": as_of,
        "revision": revision,
        "sources": sources,
        "conflicts": conflicts,
        "skipped": skipped,
        "notes": notes,
    }
    problems = validate_snapshot(snapshot)
    if problems:
        raise ValueError("Invalid snapshot: " + "; ".join(problems))
    # Prove the snapshot is consumable by the slice-1 math.
    rank_risks(snapshot)
    return snapshot


def _register_external_key(entity, by_external_key, conflicts, *, source):
    key = (entity.get("attributes") or {}).get("external_key")
    if not key:
        return
    previous = by_external_key.get(key)
    if previous is None:
        by_external_key[key] = {"source": source, "kind": entity["kind"], "name": entity["name"]}
        return
    if previous["kind"] != entity["kind"] or previous["name"] != entity["name"]:
        conflicts.append(
            {
                "external_key": key,
                "company_graph": previous if previous["source"] == "company_graph" else {"source": source, "kind": entity["kind"], "name": entity["name"]},
                "continuity": previous if previous["source"] == "continuity" else {"source": source, "kind": entity["kind"], "name": entity["name"]},
            }
        )
