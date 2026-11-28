"""Twin snapshot builder (Step S slice 2).

Builds the plain-dict twin snapshot that ``app.digital_twin.impact``
simulates over, from already-authorized sources — without changing any
of them:

- an authorized ``app.company_graph.service.projection`` (entities +
  relationships the caller may see; hidden records are never in the
  input, so they can never leak into the snapshot),
- authorized continuity nodes/links (``bc_nodes`` / ``bc_links``),
- finance invoice batches / payments (amounts are exposure figures),
- live ``WorkflowRun`` rows (freshness only — the twin never drives a
  run).

The builder is a read-only adapter: it takes plain dicts and returns a
plain dict. It writes nothing, creates no tables, runs no migration,
grants nothing, and never pays. The snapshot is validated with the same
rules slice 1 simulates over, so a snapshot this module returns is
always consumable by :func:`app.digital_twin.impact.simulate_absence`.

Boundaries (deliberate, see ``docs/step-s-digital-twin-plan.md`` §3):

- The company graph and the continuity graph are still two graphs.
  The builder merges them into *one snapshot id space* only: a
  continuity node whose ``external_key`` matches a company-graph
  entity's ``external_key`` is folded into that entity instead of
  creating a second twin node. The real graph merge (one entity id,
  one scope) is Step G's job — this module never writes either graph.
- The caller must have authorized the finance ``group_id`` (via
  ``app/platform`` ``finance.read``) and the continuity tenant before
  calling :func:`collect_snapshot_inputs`; the collector itself only
  enforces company-graph visibility, through ``projection``.
- Money amounts come from the finance rows as given; a batch total is
  the sum of its invoice amounts (``FinInvoiceBatch`` stores no total).
  Amounts must be numeric and >= 0, like slice 1 requires.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.company_graph import service as graph_service
from app.continuity.models import Link as ContinuityLink
from app.continuity.models import Node as ContinuityNode
from app.db.finance_models import FinInvoice, FinInvoiceBatch, FinPayment
from app.db.workflow_engine_models import WorkflowRun
from app.digital_twin.impact import _validate_snapshot

# Company-graph / continuity kinds that are people or teams in twin terms.
KIND_MAP = {"employee": "person", "department": "team"}

# Relationship kinds the twin snapshot carries through unchanged.
# ``impact.simulate_absence`` understands owns / responsible_for /
# operates / approves / depends_on / supplies / backup_for / member_of;
# anything else is present for other consumers and ignored by the math.
MONEY_ENTITY_KINDS = frozenset({"invoice_batch", "payment"})


def _twin_kind(kind):
    return KIND_MAP.get(str(kind), str(kind))


def _amount(value, *, where):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Amount at {where} must be a number, got {value!r}.") from None
    if amount < 0:
        raise ValueError(f"Amount at {where} must be >= 0, got {value!r}.")
    return float(amount)


def _str_id(value, *, where):
    if value is None:
        raise ValueError(f"Missing id at {where}.")
    text = str(value)
    if not text:
        raise ValueError(f"Missing id at {where}.")
    return text


def source_freshness(last_synced_at, stale_after_days, *, now=None):
    """Freshness of one twin source.

    Returns ``{"status": "fresh" | "stale" | "unknown",
    "last_synced_at": iso | None, "stale_after_days": int | None}``.
    ``last_synced_at`` may be a datetime, an ISO string, or None (never
    synced -> "unknown"). A source is stale when its last sync is older
    than ``stale_after_days``; a non-positive ``stale_after_days`` means
    "always stale once synced".
    """
    moment = now or datetime.now(UTC)
    if isinstance(last_synced_at, str):
        try:
            last_synced_at = datetime.fromisoformat(last_synced_at)
        except ValueError:
            raise ValueError("last_synced_at must be an ISO-8601 datetime string.") from None
    if last_synced_at is not None and last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=UTC)
    if stale_after_days is not None and (isinstance(stale_after_days, bool) or not isinstance(stale_after_days, int)):
        raise ValueError("stale_after_days must be an integer or None.")
    if last_synced_at is None:
        status = "unknown"
    elif stale_after_days is None:
        status = "fresh"
    elif stale_after_days <= 0 or (moment - last_synced_at).days >= stale_after_days:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "last_synced_at": last_synced_at.isoformat() if last_synced_at is not None else None,
        "stale_after_days": stale_after_days,
    }


def _graph_entity(record):
    """One company-graph projection record -> twin entity (or None)."""
    if record.get("record_type") != "entity":
        return None
    attributes = dict(record.get("attributes") or {})
    attributes.setdefault("source", "company_graph")
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


def _graph_relationship(record, entity_ids):
    if record.get("record_type") != "relationship":
        return None
    source, target = str(record.get("source_id") or ""), str(record.get("target_id") or "")
    if source not in entity_ids or target not in entity_ids:
        # projection only returns edges between visible entities, but a
        # caller-built input may not; never emit a dangling edge.
        return None
    return {
        "id": _str_id(record.get("id"), where="company-graph relationship"),
        "source_id": source,
        "target_id": target,
        "kind": str(record.get("kind")),
        "confidence": record.get("confidence", 1.0),
        "evidence": list(record.get("evidence") or []),
    }


def build_snapshot(*, graph_entities=(), graph_relationships=(), continuity_nodes=(),
                   continuity_links=(), invoice_batches=(), payments=(),
                   workflow_runs=(), as_of=None, sources=(), truncated=False):
    """Merge authorized sources into one twin snapshot dict.

    All inputs are plain dicts (or row-like objects exposing the same
    attribute names for finance/workflow rows — see
    :func:`collect_snapshot_inputs`). Every id is namespaced by source
    except company-graph ids, which are the twin's canonical ids;
    a continuity node whose ``external_key`` matches a graph entity's
    is folded into that entity instead of duplicated.

    Finance rows: ``invoice_batches`` are dicts with ``id`` / ``name`` /
    ``status`` / ``currency`` / ``total_amount`` and optional
    ``approver_ids`` (person ids in twin id space) and ``wf_run_id``;
    ``payments`` are dicts with ``id`` / ``batch_id`` / ``amount`` /
    ``currency`` / ``status`` and optional ``approver_id``.
    ``workflow_runs`` are dicts with ``id`` / ``status`` / ``phase`` /
    ``origin`` and optional ``batch_id``.

    ``sources`` is a list of ``{"name", "kind", "last_synced_at",
    "stale_after_days"}`` dicts; each is freshness-checked and the
    snapshot reports ``stale_sources`` (names). The result always
    validates under slice 1's snapshot rules (duplicate ids, dangling
    edges, negative/non-numeric money amounts all raise ValueError).
    """
    moment = as_of or datetime.now(UTC).isoformat()
    entities, by_id = [], {}

    def _add(entity):
        if entity["id"] in by_id:
            raise ValueError(f"Duplicate entity id in snapshot: {entity['id']!r}.")
        by_id[entity["id"]] = entity
        entities.append(entity)
        return entity

    # 1. Company graph first: its ids are canonical.
    graph_ids = set()
    external_to_graph = {}
    for record in graph_entities or []:
        entity = _graph_entity(record)
        if entity is None:
            continue
        _add(entity)
        graph_ids.add(entity["id"])
        external_key = entity["attributes"].get("external_key")
        if external_key:
            external_to_graph[str(external_key)] = entity["id"]

    relationships = []
    for record in graph_relationships or []:
        edge = _graph_relationship(record, graph_ids)
        if edge is not None:
            relationships.append(edge)

    # 2. Continuity nodes: fold into the matching graph entity by
    # external_key, otherwise add as bc:<id>.
    bc_id = {}
    for node in continuity_nodes or []:
        data = node.get("data") if isinstance(node, dict) else getattr(node, "data", None)
        data = dict(data or {})
        external_key = node.get("external_key") if isinstance(node, dict) else getattr(node, "external_key", None)
        twin_id = external_to_graph.get(str(external_key)) if external_key else None
        attributes = dict(data)
        attributes.setdefault("source", "continuity")
        if external_key:
            attributes.setdefault("external_key", str(external_key))
        if twin_id is not None:
            merged = dict(by_id[twin_id]["attributes"])
            merged.update({k: v for k, v in attributes.items() if k not in merged})
            by_id[twin_id]["attributes"] = merged
            bc_id[_str_id(node.get("id"), where="continuity node")] = twin_id
            continue
        node_id = f"bc:{_str_id(node.get('id'), where='continuity node')}"
        kind = _twin_kind(node.get("kind"))
        name = str(node.get("name") or node_id)
        entity = {"id": node_id, "kind": kind, "name": name, "attributes": attributes}
        _add(entity)
        bc_id[_str_id(node.get("id"), where="continuity node")] = node_id

    for link in continuity_links or []:
        source = bc_id.get(_str_id(link.get("source_id"), where="continuity link"))
        target = bc_id.get(_str_id(link.get("target_id"), where="continuity link"))
        if source is None or target is None:
            continue  # link endpoint not in the authorized snapshot
        data = link.get("data") if isinstance(link, dict) else getattr(link, "data", None)
        relationships.append({
            "id": f"bc-link:{_str_id(link.get('id'), where='continuity link')}",
            "source_id": source,
            "target_id": target,
            "kind": str(link.get("kind")),
            "confidence": float((data or {}).get("confidence", 1.0)),
            "evidence": list((data or {}).get("evidence") or []),
        })

    # 3. Finance money flow (exposure only — never an instruction).
    batch_ids = {}
    for batch in invoice_batches or []:
        batch_id = f"fin-batch:{_str_id(batch.get('id'), where='invoice batch')}"
        total = _amount(batch.get("total_amount"), where=f"invoice batch {batch_id!r}")
        currency = str(batch.get("currency") or "unspecified")
        _add({
            "id": batch_id,
            "kind": "invoice_batch",
            "name": str(batch.get("name") or batch_id),
            "attributes": {
                "amount": total, "currency": currency,
                "status": str(batch.get("status") or "open"),
                "source": "finance",
            },
        })
        batch_ids[_str_id(batch.get("id"), where="invoice batch")] = batch_id
        for approver in batch.get("approver_ids") or []:
            if str(approver) in by_id:
                relationships.append({
                    "id": f"fin-approves:{batch_id}:{approver}",
                    "source_id": str(approver), "target_id": batch_id,
                    "kind": "approves", "confidence": 1.0, "evidence": [],
                })
        wf_run = batch.get("wf_run_id")
        if wf_run:
            relationships.append({
                "id": f"fin-run:{batch_id}",
                "source_id": f"wf-run:{wf_run}", "target_id": batch_id,
                "kind": "depends_on", "confidence": 1.0, "evidence": [],
            })

    for payment in payments or []:
        payment_id = f"fin-pay:{_str_id(payment.get('id'), where='payment')}"
        amount = _amount(payment.get("amount"), where=f"payment {payment_id!r}")
        _add({
            "id": payment_id,
            "kind": "payment",
            "name": str(payment.get("name") or payment_id),
            "attributes": {
                "amount": amount, "currency": str(payment.get("currency") or "unspecified"),
                "status": str(payment.get("status") or "awaiting_approval"),
                "source": "finance",
            },
        })
        batch_ref = payment.get("batch_id")
        if batch_ref is not None and str(batch_ref) in batch_ids:
            relationships.append({
                "id": f"fin-settles:{batch_ids[str(batch_ref)]}:{payment_id}",
                "source_id": payment_id, "target_id": batch_ids[str(batch_ref)],
                "kind": "depends_on", "confidence": 1.0, "evidence": [],
            })
        approver = payment.get("approver_id")
        if approver and str(approver) in by_id:
            relationships.append({
                "id": f"fin-approves:{payment_id}:{approver}",
                "source_id": str(approver), "target_id": payment_id,
                "kind": "approves", "confidence": 1.0, "evidence": [],
            })

    # 4. Workflow runs: freshness nodes. A run that depends on a batch
    # is at risk when the batch is; a run never drives anything here.
    run_ids = set()
    for run in workflow_runs or []:
        run_id = f"wf-run:{_str_id(run.get('id'), where='workflow run')}"
        if run_id in by_id:
            run_ids.add(run_id)
            continue
        _add({
            "id": run_id,
            "kind": "workflow",
            "name": str(run.get("name") or run_id),
            "attributes": {
                "status": str(run.get("status") or "queued"),
                "phase": str(run.get("phase") or "plan"),
                "origin": str(run.get("origin") or "workflow"),
                "source": "workflow",
            },
        })
        run_ids.add(run_id)
        batch_ref = run.get("batch_id")
        if batch_ref is not None and str(batch_ref) in batch_ids:
            relationships.append({
                "id": f"wf-depends:{run_id}:{batch_ids[str(batch_ref)]}",
                "source_id": run_id, "target_id": batch_ids[str(batch_ref)],
                "kind": "depends_on", "confidence": 1.0, "evidence": [],
            })

    # Drop edges whose endpoints never materialised (e.g. a batch's
    # wf_run_id with no matching run row) — a snapshot must not dangle.
    entity_ids = set(by_id)
    relationships = [e for e in relationships
                     if e["source_id"] in entity_ids and e["target_id"] in entity_ids]
    # Deterministic order: snapshot consumers (and the fingerprint)
    # must not depend on input row order.
    entities.sort(key=lambda e: e["id"])
    relationships.sort(key=lambda e: (e["id"], e["source_id"], e["target_id"]))

    checked_sources = []
    for source in sources or []:
        info = source_freshness(source.get("last_synced_at"), source.get("stale_after_days"))
        checked_sources.append({"name": str(source.get("name") or "unknown"),
                                "kind": str(source.get("kind") or "unknown"), **info})
    snapshot = {
        "entities": entities,
        "relationships": relationships,
        "as_of": moment if isinstance(moment, str) else moment.isoformat(),
        "truncated": bool(truncated),
        "sources": checked_sources,
        "stale_sources": [s["name"] for s in checked_sources if s["status"] == "stale"],
        "fingerprint": hashlib.sha256(
            json.dumps({"entities": entities, "relationships": relationships}, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }
    _validate_snapshot(snapshot)  # must stay consumable by slice 1
    return snapshot


async def collect_snapshot_inputs(db, graph_scope, *, continuity_tenant_id=None,
                                  group_id=None, limit=500):
    """Fetch the authorized inputs for :func:`build_snapshot`.

    Company-graph rows come from ``graph_service.projection`` (the
    caller's authorized projection — hidden records are excluded by
    construction). Continuity rows are read for the explicitly given
    ``continuity_tenant_id``; finance and workflow rows for the given
    platform ``group_id``. The caller must have authorized both ids
    through ``app/platform`` before calling (finance.read on the group,
    continuity membership on the tenant); this function does not invent
    that authorization. Returns plain dicts ready for ``build_snapshot``.
    """
    projection = await graph_service.projection(db, graph_scope, limit=limit)
    inputs = {
        "graph_entities": projection["entities"],
        "graph_relationships": projection["relationships"],
        "continuity_nodes": [],
        "continuity_links": [],
        "invoice_batches": [],
        "payments": [],
        "workflow_runs": [],
        "truncated": bool(projection.get("truncated")),
        "sources": [{"name": "company_graph", "kind": "company_graph",
                     "last_synced_at": projection.get("as_of"), "stale_after_days": None}],
    }
    if continuity_tenant_id is not None:
        nodes = list((await db.scalars(select(ContinuityNode).where(
            ContinuityNode.tenant_id == continuity_tenant_id))).all())
        links = list((await db.scalars(select(ContinuityLink).where(
            ContinuityLink.tenant_id == continuity_tenant_id))).all())
        inputs["continuity_nodes"] = [
            {"id": n.id, "kind": n.kind, "name": n.name,
             "external_key": n.external_key, "data": dict(n.data or {})} for n in nodes]
        inputs["continuity_links"] = [
            {"id": l.id, "source_id": l.source_id, "target_id": l.target_id,
             "kind": l.kind, "data": dict(l.data or {})} for l in links]
        inputs["sources"].append({"name": "continuity", "kind": "continuity",
                                  "last_synced_at": None, "stale_after_days": None})
    if group_id is not None:
        batches = list((await db.scalars(select(FinInvoiceBatch).where(
            FinInvoiceBatch.group_id == group_id))).all())
        totals = {}
        if batches:
            rows = list((await db.scalars(select(FinInvoice).where(
                FinInvoice.batch_id.in_([b.id for b in batches])))).all())
            for invoice in rows:
                totals[invoice.batch_id] = totals.get(invoice.batch_id, Decimal("0")) + (invoice.amount or Decimal("0"))
        payments = list((await db.scalars(select(FinPayment).where(
            FinPayment.group_id == group_id))).all())
        runs = list((await db.scalars(select(WorkflowRun).where(
            WorkflowRun.scope_id == group_id))).all())
        inputs["invoice_batches"] = [
            {"id": b.id, "name": b.name, "status": b.status, "currency": b.currency,
             "total_amount": totals.get(b.id, Decimal("0")), "wf_run_id": b.wf_run_id}
            for b in batches]
        inputs["payments"] = [
            {"id": p.id, "batch_id": p.batch_id, "amount": p.amount,
             "currency": p.currency, "status": p.status} for p in payments]
        inputs["workflow_runs"] = [
            {"id": r.id, "status": r.status, "phase": r.phase, "origin": r.origin}
            for r in runs]
        inputs["sources"].append({"name": "finance", "kind": "finance",
                                  "last_synced_at": None, "stale_after_days": None})
    return inputs


async def build_twin_snapshot(db, graph_scope, *, continuity_tenant_id=None,
                              group_id=None, limit=500, sources=()):
    """Collect authorized inputs and build the twin snapshot (read-only)."""
    inputs = await collect_snapshot_inputs(
        db, graph_scope, continuity_tenant_id=continuity_tenant_id,
        group_id=group_id, limit=limit)
    extra_sources = list(inputs.pop("sources"))
    extra_sources.extend(sources or [])
    return build_snapshot(**inputs, sources=extra_sources)
