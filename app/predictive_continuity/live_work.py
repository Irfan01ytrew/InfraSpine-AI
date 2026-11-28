"""Live-work wiring for forecasts (Step L slice 5).

Slice 1 names live work in the forecast explanation — workflow runs,
invoice batches, payments and approvals in flight that are linked to
each affected process — but it reads them only from the twin snapshot
the operator supplies. This module feeds **real** live work from the
application's own tables into that explanation, and makes the scan
refresh when live work changes, not only when the graph changes.

Two linkage paths, both advisory-only (a forecast never approves,
dispatches, pays, or changes anything):

1. **Snapshot refresh** (:func:`refresh_snapshot_live_work`) — the
   snapshot owns the graph: a live-work entity in the snapshot is
   matched to a real database row by ``(kind, id)``, and the row's
   live ``status`` replaces the snapshot's stale one. The existing
   slice-1 math then links the refreshed entities to processes
   exactly as before. Rows with no snapshot entity are reported as
   ``added`` (never inserted — inserting would invent graph edges);
   snapshot entities with no database row are reported as ``stale``
   (kept with their snapshot status — the row may have been created
   after the load, or the snapshot may be ahead of the tables).
   Contract: the snapshot builder must use the database row id as
   the snapshot entity id for the match to work (that is what
   ``--from-twin`` will do when Step S merges).

2. **Explicit attach** (:func:`attach_live_work`) — a caller that
   already knows which process a live-work item belongs to passes
   ``process_id`` on the item; the item lands in that process's
   ``live_work_items`` (live status wins over a same ``(kind, id)``
   entry already there). Items naming a process the forecast does
   not contain are reported as ``unmatched``, never silently
   dropped.

Reading: the loaders read the real tables **by reflection** — no
imports of the source modules, so the wiring works on this branch
(where Step S is not merged) and after the merge, unchanged:

- ``wf_runs`` (unified workflow engine; ``wf_definitions`` for the
  name) + ``automation_runs`` (legacy automation mirror;
  ``automation_workflows`` for the name) — in-flight runs only.
- ``platform_approval_requests`` — ``pending`` approvals only.
- ``fin_invoice_batches`` — in-flight batches only.
- ``fin_payments`` — in-flight payments only.

The status sets below are verbatim from each table's database
CHECK; anything outside a set is a schema change, not a live-work
state, and fails loudly in the mapper. All loads are read-only,
capped per source, and scoped by the snapshot itself: only items
matching a snapshot entity ever reach a forecast, so the snapshot
is the tenant boundary (the tables themselves are scoped by
department / platform scope, not by graph tenant).

Refresh gating: :func:`live_work_refresh_needed` is true when the
twin fingerprint **or** the live-work fingerprint changed. The
scheduler stores the refreshed snapshot's fingerprint, so a scan
re-fires when live work changes even if the snapshot file did not.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.ext.asyncio import AsyncSession

from app.predictive_continuity.forecast import LIVE_WORK_KINDS

__all__ = [
    "APPROVAL_IN_FLIGHT",
    "APPROVALS_TABLE",
    "AUTOMATION_RUNS_TABLE",
    "AUTOMATION_RUN_IN_FLIGHT",
    "AUTOMATION_WORKFLOWS_TABLE",
    "BATCHES_TABLE",
    "BATCH_IN_FLIGHT",
    "LIVE_WORK_LOAD_LIMIT",
    "MAX_LIVE_WORK_ITEMS",
    "PAYMENTS_TABLE",
    "PAYMENT_IN_FLIGHT",
    "WF_DEFINITIONS_TABLE",
    "WF_RUNS_TABLE",
    "WF_RUN_IN_FLIGHT",
    "LiveWorkUnavailable",
    "attach_live_work",
    "live_work_changes",
    "live_work_fingerprint",
    "live_work_refresh_needed",
    "load_automation_runs",
    "load_invoice_batches",
    "load_live_work",
    "load_payments",
    "load_pending_approvals",
    "load_wf_runs",
    "load_workflow_runs",
    "normalize_live_work_item",
    "normalize_live_work_items",
    "refresh_forecast_snapshot",
    "refresh_snapshot_live_work",
    "summarize_live_work",
]

#: Cap on live-work items in one normalized list. A scan with more
#: in-flight work than this needs a bounded follow-up, not a silent
#: truncation the caller cannot see.
MAX_LIVE_WORK_ITEMS = 500

#: Per-source read cap, matching AG's own list cap (200 rows).
LIVE_WORK_LOAD_LIMIT = 200

#: The live-work tables slice 5 reads. Stable on this base
#: (``ca956c2``) — if a table is renamed, the loader fails loudly
#: instead of reading the wrong table.
WF_RUNS_TABLE = "wf_runs"
WF_DEFINITIONS_TABLE = "wf_definitions"
AUTOMATION_RUNS_TABLE = "automation_runs"
AUTOMATION_WORKFLOWS_TABLE = "automation_workflows"
APPROVALS_TABLE = "platform_approval_requests"
BATCHES_TABLE = "fin_invoice_batches"
PAYMENTS_TABLE = "fin_payments"

#: In-flight statuses, verbatim from each table's CHECK constraint.
WF_RUN_IN_FLIGHT = frozenset({"queued", "running", "paused", "compensating"})
AUTOMATION_RUN_IN_FLIGHT = frozenset({"pending_approval", "queued", "processing"})
APPROVAL_IN_FLIGHT = frozenset({"pending"})
BATCH_IN_FLIGHT = frozenset({"open", "verifying", "awaiting_approval", "paying"})
PAYMENT_IN_FLIGHT = frozenset({"awaiting_approval", "approved", "dispatched"})

#: Columns each loader reads. Pinned against the real models by
#: tests (test_forecast_live_work.py::test_loader_columns_match_models).
_WF_RUN_COLUMNS = frozenset(
    {"id", "workflow_id", "scope_id", "status", "deadline_at", "created_at"})
_WF_DEFINITION_COLUMNS = frozenset({"id", "name"})
_AUTOMATION_RUN_COLUMNS = frozenset(
    {"id", "workflow_id", "status", "created_at"})
_AUTOMATION_WORKFLOW_COLUMNS = frozenset({"id", "name"})
_APPROVAL_COLUMNS = frozenset(
    {"id", "action_type", "subject", "amount", "currency", "status",
     "expires_at", "created_at"})
_BATCH_COLUMNS = frozenset({"id", "name", "status", "currency", "created_at"})
_PAYMENT_COLUMNS = frozenset(
    {"id", "batch_id", "amount", "currency", "status", "created_at"})

#: Optional keys a normalized live-work item may carry.
_ITEM_FIELDS = frozenset({
    "kind", "id", "name", "status", "process_id", "due_at",
    "amount", "currency", "subject", "source",
})


class LiveWorkUnavailable(RuntimeError):
    """A live-work table is missing or its shape changed.

    Raised when a table the loader reads does not exist on this
    database or is missing a column. A deployment/merge state, not
    a bad input.
    """


# -------------------------------------------------------------------------------------- normalize


def _coerce_due_at(value, *, field: str = "due_at") -> str | None:
    """ISO date / datetime / date → ISO string, or ValueError.

    A bare date stays a bare date (``"2026-12-02"``), matching how
    slice 1 reports ``due_at``; a datetime keeps its time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            pass
    raise ValueError(f"Live-work {field} must be an ISO date/datetime, got {value!r}.")


def _coerce_amount(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"Live-work amount must be a number, got {value!r}.")
    amount = float(value)
    if amount < 0:
        raise ValueError(f"Live-work amount must be non-negative, got {value!r}.")
    return amount


def _clean_text(value, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Live-work {field} must be a string, got {value!r}.")
    cleaned = value.strip()
    return cleaned or None


def normalize_live_work_item(item: dict) -> dict:
    """Validate one live-work item onto the canonical dict shape.

    Required: ``kind`` in :data:`LIVE_WORK_KINDS`, ``id`` a
    non-empty string. Optional: ``name``, ``status``,
    ``process_id``, ``due_at`` (ISO date/datetime), ``amount``
    (non-negative number, stored as float), ``currency`` (3
    letters, upper-cased), ``subject``, ``source``. Unknown keys
    raise ``ValueError`` — a typo or schema drift must fail loudly,
    not ride along into a forecast.
    """
    if not isinstance(item, dict):
        raise ValueError(f"Live-work item must be a dict, got {item!r}.")
    unknown = set(item) - _ITEM_FIELDS
    if unknown:
        raise ValueError(f"Live-work item has unknown fields {sorted(unknown)}.")
    kind = item.get("kind")
    if kind not in LIVE_WORK_KINDS:
        raise ValueError(
            f"Live-work kind must be one of {sorted(LIVE_WORK_KINDS)}, got {kind!r}.")
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(f"Live-work item needs a non-empty string id, got {raw_id!r}.")
    currency = item.get("currency")
    if currency is not None:
        if not isinstance(currency, str) or len(currency.strip()) != 3 \
                or not currency.strip().isalpha():
            raise ValueError(
                f"Live-work currency must be a 3-letter code, got {currency!r}.")
        currency = currency.strip().upper()
    return {
        "kind": kind,
        "id": raw_id.strip(),
        "name": _clean_text(item.get("name"), field="name"),
        "status": _clean_text(item.get("status"), field="status"),
        "process_id": _clean_text(item.get("process_id"), field="process_id"),
        "due_at": _coerce_due_at(item.get("due_at")),
        "amount": _coerce_amount(item.get("amount")),
        "currency": currency,
        "subject": _clean_text(item.get("subject"), field="subject"),
        "source": _clean_text(item.get("source"), field="source"),
    }


def normalize_live_work_items(items) -> list[dict]:
    """Validate + sort live-work items (by kind, name, id).

    Raises ``ValueError`` for a non-list, a malformed item, a
    duplicate ``(kind, id)``, or more than
    :data:`MAX_LIVE_WORK_ITEMS` items.
    """
    if items is None:
        raise ValueError("Live-work items must be a list, got None.")
    if not isinstance(items, list):
        raise ValueError(f"Live-work items must be a list, got {type(items).__name__}.")
    if len(items) > MAX_LIVE_WORK_ITEMS:
        raise ValueError(
            f"Live-work items exceed the {MAX_LIVE_WORK_ITEMS}-item cap "
            f"({len(items)} given).")
    normalized = [normalize_live_work_item(item) for item in items]
    seen = set()
    for item in normalized:
        key = (item["kind"], item["id"])
        if key in seen:
            raise ValueError(f"Duplicate live-work item {key}.")
        seen.add(key)
    normalized.sort(key=lambda i: (i["kind"], i["name"] or "", i["id"]))
    return normalized


# -------------------------------------------------------------------------------------- fingerprint / diff


def live_work_fingerprint(items) -> str:
    """SHA-256 over the canonical JSON of normalized live-work items.

    Same approach as the slice-1 snapshot fingerprint: order- and
    whitespace-independent, so the same live work always hashes the
    same, and any status (or other field) change hashes differently.
    """
    normalized = normalize_live_work_items(items)
    payload = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def live_work_changes(previous, current) -> dict:
    """Diff two live-work lists by ``(kind, id)``.

    Returns ``{"added": [...], "removed": [...], "changed": [...]}``
    — added/removed are full normalized items, changed entries are
    ``{"kind", "id", "fields", "previous_status", "current_status"}``
    naming the fields that differ. All three lists are sorted by
    (kind, name, id) / (kind, id) for determinism.
    """
    old = {(i["kind"], i["id"]): i for i in normalize_live_work_items(previous)}
    new = {(i["kind"], i["id"]): i for i in normalize_live_work_items(current)}
    added = [new[key] for key in sorted(new, key=lambda k: (k[0], new[k]["name"] or "", k[1]))
             if key not in old]
    removed = [old[key] for key in sorted(old, key=lambda k: (k[0], old[k]["name"] or "", k[1]))
               if key not in new]
    changed = []
    for key in sorted(set(old) & set(new)):
        before, after = old[key], new[key]
        fields = sorted(f for f in before if before[f] != after[f])
        if fields:
            changed.append({
                "kind": key[0],
                "id": key[1],
                "fields": fields,
                "previous_status": before["status"],
                "current_status": after["status"],
            })
    return {"added": added, "removed": removed, "changed": changed}


def live_work_refresh_needed(previous_twin_fingerprint: str | None,
                             twin_fingerprint: str | None,
                             previous_live_work_fingerprint: str | None,
                             live_work_fingerprint_value: str | None) -> bool:
    """True when the twin OR the live work changed since last scan.

    A ``None`` previous fingerprint means "never scanned (with live
    work)" — the refresh is needed once to establish the baseline.
    This is the slice-5 rule: a forecast refreshes when live work
    changes, not only when the graph changes.
    """
    return (twin_fingerprint != previous_twin_fingerprint
            or live_work_fingerprint_value != previous_live_work_fingerprint)


def summarize_live_work(items) -> dict:
    """Counts only — no names, no amounts — for scan summaries."""
    normalized = normalize_live_work_items(items)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in normalized:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        status = item["status"] or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
    return {"total": len(normalized), "by_kind": by_kind, "by_status": by_status}


# -------------------------------------------------------------------------------------- attach


def attach_live_work(forecast: dict, items) -> dict:
    """Attach live-work items to a slice-1 forecast's processes.

    Items carry ``process_id``; an item lands in that process's
    ``live_work_items`` as ``{"id", "name", "kind", "status"}``
    (name falls back to the id, exactly like slice 1). A live item
    with the same ``(kind, id)`` as an entry already there replaces
    it — the database is the live authority. Items with no
    ``process_id`` or naming a process the forecast does not
    contain are reported as ``unmatched``, never silently dropped.

    Returns ``{"forecast", "attached", "unmatched"}``; the input
    forecast is never mutated. Raises ``ValueError`` for a forecast
    without a ``processes`` list or a malformed item.
    """
    if not isinstance(forecast, dict) or not isinstance(forecast.get("processes"), list):
        raise ValueError("Forecast must be a dict with a 'processes' list.")
    normalized = normalize_live_work_items(items)
    result = copy.deepcopy(forecast)
    by_process = {proc["id"]: proc for proc in result["processes"]
                  if isinstance(proc, dict) and "id" in proc}
    attached = 0
    unmatched = []
    for item in normalized:
        process_id = item["process_id"]
        proc = by_process.get(process_id) if process_id else None
        if proc is None:
            unmatched.append(item)
            continue
        entry = {"id": item["id"], "name": item["name"] or item["id"],
                 "kind": item["kind"], "status": item["status"]}
        entries = proc.setdefault("live_work_items", [])
        existing = {(e.get("kind"), e.get("id")): idx
                    for idx, e in enumerate(entries) if isinstance(e, dict)}
        key = (entry["kind"], entry["id"])
        if key in existing:
            entries[existing[key]] = entry
        else:
            entries.append(entry)
        entries.sort(key=lambda e: (e.get("kind", ""), e.get("name", ""), e.get("id", "")))
        attached += 1
    return {"forecast": result, "attached": attached, "unmatched": unmatched}


# -------------------------------------------------------------------------------------- snapshot refresh


def _snapshot_live_work_items(snapshot: dict) -> list[dict]:
    """Build normalized items from a snapshot's live-work entities."""
    entities = snapshot.get("entities")
    if not isinstance(entities, list):
        raise ValueError("Snapshot must be a dict with an 'entities' list.")
    items = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("kind") not in LIVE_WORK_KINDS:
            continue
        attributes = entity.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise ValueError(
                f"Snapshot entity {entity.get('id')!r} has non-dict attributes.")
        items.append(normalize_live_work_item({
            "kind": entity["kind"],
            "id": str(entity.get("id")),
            "name": attributes.get("name") if "name" in attributes else entity.get("name"),
            "status": attributes.get("status"),
            "due_at": attributes.get("due_at"),
            "amount": attributes.get("amount"),
            "currency": attributes.get("currency"),
        }))
    return normalize_live_work_items(items)


def refresh_snapshot_live_work(snapshot: dict, db_items) -> dict:
    """Refresh a snapshot's live-work entities from real rows.

    Matches each snapshot live-work entity to a database item by
    ``(kind, id)`` and replaces the entity's ``attributes.status``
    with the row's live status (only when the row carries one and
    it differs — the status is the live state; names, edges and
    amounts stay snapshot-owned). Returns ``{"snapshot",
    "updated", "added", "stale", "matched", "fingerprint_before",
    "fingerprint_after"}``:

    - ``updated`` — ``{"kind", "id", "previous_status", "status"}``
      per entity whose status actually changed.
    - ``added`` — database items with no snapshot entity (reported,
      never inserted: the snapshot owns the graph).
    - ``stale`` — snapshot live-work entities with no database row
      (kept with their snapshot status).
    - fingerprints — :func:`live_work_fingerprint` of the snapshot's
      live-work entities before and after the refresh.

    The input snapshot is never mutated. Raises ``ValueError`` for a
    malformed snapshot or item.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict.")
    normalized = normalize_live_work_items(db_items)
    by_key = {(item["kind"], item["id"]): item for item in normalized}
    before_items = _snapshot_live_work_items(snapshot)
    fingerprint_before = live_work_fingerprint(before_items)
    result = copy.deepcopy(snapshot)
    snapshot_keys = {(entity["kind"], str(entity.get("id")))
                     for entity in result.get("entities", [])
                     if isinstance(entity, dict)
                     and entity.get("kind") in LIVE_WORK_KINDS}
    updated = []
    matched = 0
    for entity in result.get("entities", []):
        if not isinstance(entity, dict) or entity.get("kind") not in LIVE_WORK_KINDS:
            continue
        key = (entity["kind"], str(entity.get("id")))
        item = by_key.get(key)
        if item is None:
            continue
        matched += 1
        db_status = item["status"]
        if db_status is None:
            continue
        attributes = entity.setdefault("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError(
                f"Snapshot entity {entity.get('id')!r} has non-dict attributes.")
        previous_status = attributes.get("status")
        if previous_status != db_status:
            attributes["status"] = db_status
            updated.append({"kind": key[0], "id": key[1],
                            "previous_status": previous_status,
                            "status": db_status})
    added = [item for item in normalized if (item["kind"], item["id"]) not in snapshot_keys]
    stale = [{"kind": item["kind"], "id": item["id"], "status": item["status"]}
             for item in before_items
             if (item["kind"], item["id"]) not in by_key]
    fingerprint_after = live_work_fingerprint(_snapshot_live_work_items(result))
    return {
        "snapshot": result,
        "updated": updated,
        "added": added,
        "stale": stale,
        "matched": matched,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
    }


# -------------------------------------------------------------------------------------- loading


def _reflect_table(sync_session, table_name: str, columns: frozenset) -> Table:
    """Reflect one table; fail loudly when it (or a column) is absent."""
    metadata = MetaData()
    try:
        table = Table(table_name, metadata, autoload_with=sync_session.get_bind())
    except NoSuchTableError as exc:
        raise LiveWorkUnavailable(
            f"Live-work table {table_name!r} is missing on this database.") from exc
    missing = columns - set(table.columns.keys())
    if missing:
        raise LiveWorkUnavailable(
            f"Live-work table {table_name!r} is missing columns "
            f"{sorted(missing)} — the schema changed; update the loader.")
    return table


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}.")


async def _load_names(session: AsyncSession, table_name: str,
                      columns: frozenset, ids) -> dict:
    """Map row ids to names from a definition table (empty input → {})."""
    ids = [i for i in dict.fromkeys(ids)]
    if not ids:
        return {}
    table = await session.run_sync(_reflect_table, table_name, columns)
    rows = (await session.execute(
        select(table.c.id, table.c.name).where(table.c.id.in_(ids)))).mappings().all()
    return {str(row["id"]): row["name"] for row in rows}


async def load_wf_runs(session: AsyncSession, *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """In-flight unified-engine runs (``wf_runs``) as live-work items.

    Read-only, newest last (oldest first — the runs that have been
    waiting longest surface first), capped. Names come from
    ``wf_definitions``. ``due_at`` is the run's ``deadline_at`` when
    set. Terminal runs (completed / failed / cancelled /
    compensated) are not in flight and are never returned.
    """
    _validate_limit(limit)
    table = await session.run_sync(_reflect_table, WF_RUNS_TABLE, _WF_RUN_COLUMNS)
    rows = (await session.execute(
        select(table.c.id, table.c.workflow_id, table.c.status,
               table.c.deadline_at, table.c.created_at)
        .where(table.c.status.in_(WF_RUN_IN_FLIGHT))
        .order_by(table.c.created_at, table.c.id).limit(limit)
    )).mappings().all()
    names = await _load_names(session, WF_DEFINITIONS_TABLE, _WF_DEFINITION_COLUMNS,
                              [row["workflow_id"] for row in rows])
    return [normalize_live_work_item({
        "kind": "workflow_run",
        "id": str(row["id"]),
        "name": names.get(str(row["workflow_id"])),
        "status": row["status"],
        "due_at": row["deadline_at"].isoformat() if row["deadline_at"] is not None else None,
        "source": WF_RUNS_TABLE,
    }) for row in rows]


async def load_automation_runs(session: AsyncSession,
                               *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """In-flight legacy automation runs (``automation_runs``).

    Same shape as :func:`load_wf_runs`; names come from
    ``automation_workflows``. The legacy mirror is a separate
    runtime from the unified engine, so its runs are listed
    alongside (``source`` tells them apart) rather than merged.
    """
    _validate_limit(limit)
    table = await session.run_sync(
        _reflect_table, AUTOMATION_RUNS_TABLE, _AUTOMATION_RUN_COLUMNS)
    rows = (await session.execute(
        select(table.c.id, table.c.workflow_id, table.c.status, table.c.created_at)
        .where(table.c.status.in_(AUTOMATION_RUN_IN_FLIGHT))
        .order_by(table.c.created_at, table.c.id).limit(limit)
    )).mappings().all()
    names = await _load_names(session, AUTOMATION_WORKFLOWS_TABLE,
                              _AUTOMATION_WORKFLOW_COLUMNS,
                              [row["workflow_id"] for row in rows])
    return [normalize_live_work_item({
        "kind": "workflow_run",
        "id": str(row["id"]),
        "name": names.get(str(row["workflow_id"])),
        "status": row["status"],
        "source": AUTOMATION_RUNS_TABLE,
    }) for row in rows]


async def load_workflow_runs(session: AsyncSession,
                             *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """All in-flight workflow runs (unified engine + legacy mirror)."""
    runs = await load_wf_runs(session, limit=limit)
    runs.extend(await load_automation_runs(session, limit=limit))
    return normalize_live_work_items(runs)


async def load_pending_approvals(session: AsyncSession,
                                 *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """Pending platform approvals (``platform_approval_requests``).

    Read-only, oldest first. The name is the approval's ``subject``
    (e.g. ``"invoice:INV-204"``) falling back to ``action_type``;
    ``due_at`` is ``expires_at``; amount/currency ride along when
    the approval carries them. Decided/expired/consumed approvals
    are not in flight and are never returned.
    """
    _validate_limit(limit)
    table = await session.run_sync(_reflect_table, APPROVALS_TABLE, _APPROVAL_COLUMNS)
    rows = (await session.execute(
        select(table.c.id, table.c.action_type, table.c.subject,
               table.c.amount, table.c.currency, table.c.status,
               table.c.expires_at, table.c.created_at)
        .where(table.c.status.in_(APPROVAL_IN_FLIGHT))
        .order_by(table.c.created_at, table.c.id).limit(limit)
    )).mappings().all()
    return [normalize_live_work_item({
        "kind": "approval",
        "id": str(row["id"]),
        "name": row["subject"] or row["action_type"],
        "status": row["status"],
        "due_at": row["expires_at"].isoformat() if row["expires_at"] is not None else None,
        "amount": row["amount"],
        "currency": row["currency"],
        "subject": row["subject"],
        "source": APPROVALS_TABLE,
    }) for row in rows]


async def load_invoice_batches(session: AsyncSession,
                               *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """In-flight invoice batches (``fin_invoice_batches``).

    Read-only, oldest first. The batch's money lives in its
    invoices/payments (separate live-work entities), so no amount
    rides here — only the batch's own live status.
    """
    _validate_limit(limit)
    table = await session.run_sync(_reflect_table, BATCHES_TABLE, _BATCH_COLUMNS)
    rows = (await session.execute(
        select(table.c.id, table.c.name, table.c.status,
               table.c.currency, table.c.created_at)
        .where(table.c.status.in_(BATCH_IN_FLIGHT))
        .order_by(table.c.created_at, table.c.id).limit(limit)
    )).mappings().all()
    return [normalize_live_work_item({
        "kind": "invoice_batch",
        "id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "currency": row["currency"],
        "source": BATCHES_TABLE,
    }) for row in rows]


async def load_payments(session: AsyncSession,
                        *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """In-flight payments (``fin_payments``).

    Read-only, oldest first. ``subject`` names the batch the
    payment belongs to (``"batch:<uuid>"``); the approval it waits
    on is a separate live-work entity (linked by ``approval_id`` in
    the database, by snapshot edges in the forecast).
    """
    _validate_limit(limit)
    table = await session.run_sync(_reflect_table, PAYMENTS_TABLE, _PAYMENT_COLUMNS)
    rows = (await session.execute(
        select(table.c.id, table.c.batch_id, table.c.amount,
               table.c.currency, table.c.status, table.c.created_at)
        .where(table.c.status.in_(PAYMENT_IN_FLIGHT))
        .order_by(table.c.created_at, table.c.id).limit(limit)
    )).mappings().all()
    return [normalize_live_work_item({
        "kind": "payment",
        "id": str(row["id"]),
        "name": None,
        "status": row["status"],
        "amount": row["amount"],
        "currency": row["currency"],
        "subject": f"batch:{row['batch_id']}",
        "source": PAYMENTS_TABLE,
    }) for row in rows]


async def load_live_work(session: AsyncSession,
                         *, limit: int = LIVE_WORK_LOAD_LIMIT) -> list[dict]:
    """All in-flight live work (runs + approvals + batches + payments).

    One normalized, sorted list, capped at
    :data:`MAX_LIVE_WORK_ITEMS` in total (each source is capped at
    ``limit`` first). Raises :class:`LiveWorkUnavailable` when any
    source table is missing, ``ValueError`` for a bad limit.
    """
    items: list[dict] = []
    items.extend(await load_workflow_runs(session, limit=limit))
    items.extend(await load_pending_approvals(session, limit=limit))
    items.extend(await load_invoice_batches(session, limit=limit))
    items.extend(await load_payments(session, limit=limit))
    return normalize_live_work_items(items)


# -------------------------------------------------------------------------------------- end to end


async def refresh_forecast_snapshot(snapshot: dict, session: AsyncSession,
                                    *, limit: int = LIVE_WORK_LOAD_LIMIT) -> dict:
    """Load real live work and refresh a snapshot with it.

    The end-to-end entry point: :func:`load_live_work` then
    :func:`refresh_snapshot_live_work`. Returns the refresh report
    (``snapshot`` is the refreshed copy). Advisory only; nothing
    here writes to the database. Raises ``LiveWorkUnavailable``
    when a source table is missing, ``ValueError`` for a malformed
    snapshot or item.
    """
    items = await load_live_work(session, limit=limit)
    return refresh_snapshot_live_work(snapshot, items)
