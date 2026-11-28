"""Standing-forecast scheduler (Step L slice 3 owed wiring + slice 4 prep + slice 5 live work).

The scan itself is pure (slice 2 :func:`scan_forecasts`); this module
is the thin, repeatable entry point that calls it and persists the
result through slice 3's :func:`record_scan`:

- load a twin snapshot (JSON file — Step S's snapshot builder does
  not exist on this base yet, so the operator supplies the snapshot;
  when S merges, a ``--from-twin`` mode can build it with
  ``build_twin_snapshot`` instead),
- optionally refresh the snapshot's live-work entities from the
  real tables (slice 5: ``--live-work`` — a scan re-fires when live
  work changes even if the snapshot file did not),
- run the fingerprint-gated scan (an unchanged twin never re-fires),
- record the forecasts (idempotent per fingerprint),
- evaluate the watch rules (quiet hours 22:00–07:00 PKT, cooldown /
  dedupe) and print the advisory alert lines,
- optionally prune superseded records past retention.

State between runs lives in a small JSON file (``--state-file``):
the last twin fingerprint (scan gating) and the alert ``sent_at``
map (cooldown/dedupe). Without a state file every run is fresh:
no gating, no dedupe. The state file is advisory bookkeeping, not
an approval or a dispatch — a forecast never acts on anything.

Safety: the scan only writes forecast records (advisory data). It
never approves, dispatches, grants access, pays, or switches a
supplier. A malformed snapshot raises ``ValueError`` and writes
nothing. ``--dry-run`` scans and evaluates the watch without
touching the database or the state file.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.company_graph.models import GraphTenant
from app.db.models import User
from app.predictive_continuity.live_work import refresh_snapshot_live_work
from app.predictive_continuity.records import (
    prune_forecasts,
    record_scan,
)
from app.predictive_continuity.watch import (
    ORG_TZ,
    evaluate_forecast_watch,
    format_watch_message,
    scan_forecasts,
)

__all__ = [
    "build_scan_report",
    "format_scan_summary",
    "load_snapshot",
    "load_state",
    "run_forecast_scan",
    "save_state",
]


# -------------------------------------------------------------------------------------- snapshot / state

def load_snapshot(path: str | Path) -> dict:
    """Read a twin snapshot JSON file (``-`` reads stdin).

    Returns the parsed dict; the strict shape validation happens in
    :func:`scan_forecasts` (malformed snapshots raise ``ValueError``
    and write nothing). Raises ``ValueError`` for unreadable files
    and non-object JSON.
    """
    if str(path) == "-":
        raw = sys.stdin.read()
        source = "stdin"
    else:
        file = Path(path)
        try:
            raw = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Cannot read snapshot file {file}: {exc}") from exc
        source = str(file)
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Snapshot {source} is not valid JSON: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise ValueError(f"Snapshot {source} must be a JSON object, got {type(snapshot).__name__}.")
    return snapshot


def load_state(path: str | Path | None) -> dict:
    """Load scheduler state (``{"fingerprint", "sent_at"}``).

    A missing file means "never scanned". A malformed file warns on
    stderr and also means "never scanned" — a corrupt state file must
    never block a scan; the scan itself is the source of truth.
    """
    fresh = {"fingerprint": None, "sent_at": {}}
    if path is None:
        return fresh
    file = Path(path)
    if not file.exists():
        return fresh
    try:
        state = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: ignoring malformed state file {file}: {exc}", file=sys.stderr)
        return fresh
    if not isinstance(state, dict):
        print(f"Warning: ignoring malformed state file {file}: not a JSON object.", file=sys.stderr)
        return fresh
    fingerprint = state.get("fingerprint")
    sent_at = state.get("sent_at") or {}
    if fingerprint is not None and not isinstance(fingerprint, str):
        return fresh
    if not isinstance(sent_at, dict):
        return fresh
    return {"fingerprint": fingerprint, "sent_at": dict(sent_at)}


def save_state(path: str | Path, state: dict) -> None:
    """Atomically write scheduler state (temp file + rename)."""
    file = Path(path)
    payload = {
        "fingerprint": state.get("fingerprint"),
        "sent_at": state.get("sent_at") or {},
    }
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(file)


# -------------------------------------------------------------------------------------- pure scan + watch

def build_scan_report(snapshot: dict, *, now: datetime | None = None,
                      critical_ids=None, horizons=None,
                      previous_fingerprint: str | None = None,
                      sent_at: dict | None = None) -> dict:
    """Run one standing scan and evaluate the watch rules (pure).

    Returns ``{"fingerprint", "refreshed", "scanned_at", "horizons",
    "critical_ids", "counts", "forecasts", "alerts", "suppressed",
    "deferred", "sent_at"}``. ``alerts`` are formatted
    plain-language lines (see :func:`format_watch_message`);
    ``suppressed`` / ``deferred`` are counts with their reasons.
    ``sent_at`` is the updated map: fired alert fingerprints stamped
    with the scan moment (cooldown/dedupe for the next run).

    When the twin fingerprint matches ``previous_fingerprint`` the
    scan returns ``refreshed: False`` with no forecasts and no
    alerts — an unchanged twin never re-fires.

    ``now`` defaults to the current org time (Asia/Karachi). The
    watch reads the wall clock of ``now`` as org time, so a UTC
    default would misjudge quiet hours (23:00 PKT is 18:00 UTC,
    daytime to the watch); callers passing an explicit ``now``
    must pass org time, not UTC.
    """
    moment = now or datetime.now(ORG_TZ)
    scan = scan_forecasts(
        snapshot, critical_ids,
        horizons=horizons, now=moment,
        previous_fingerprint=previous_fingerprint,
    )
    if not scan["refreshed"]:
        return {
            "fingerprint": scan["fingerprint"],
            "refreshed": False,
            "scanned_at": scan["scanned_at"],
            "horizons": scan["horizons"],
            "critical_ids": scan["critical_ids"],
            "counts": scan["counts"],
            "forecasts": [],
            "alerts": [],
            "suppressed": [],
            "deferred": [],
            "sent_at": dict(sent_at or {}),
        }
    parsed_sent_at = {}
    for fingerprint, when in (sent_at or {}).items():
        try:
            parsed_sent_at[fingerprint] = (
                datetime.fromisoformat(when) if isinstance(when, str) else when
            )
        except ValueError:
            continue
    watch = evaluate_forecast_watch(scan, now=moment, sent_at=parsed_sent_at)
    new_sent_at = dict(sent_at or {})
    for alert in watch["alerts"]:
        new_sent_at[alert.fingerprint] = moment.isoformat()
    return {
        "fingerprint": scan["fingerprint"],
        "refreshed": True,
        "scanned_at": scan["scanned_at"],
        "horizons": scan["horizons"],
        "critical_ids": scan["critical_ids"],
        "counts": scan["counts"],
        "forecasts": scan["forecasts"],
        "alerts": [format_watch_message(alert) for alert in watch["alerts"]],
        "suppressed": [
            {"fingerprint": entry["fingerprint"], "reason": entry["reason"]}
            for entry in watch["suppressed"]
        ],
        "deferred": [
            {"fingerprint": entry["fingerprint"], "reason": entry["reason"]}
            for entry in watch["deferred"]
        ],
        "sent_at": new_sent_at,
    }


def format_scan_summary(summary: dict) -> str:
    """One deterministic plain-language summary of a scan pass."""
    counts = summary.get("counts") or {}
    lines = [
        f"forecast scan {summary.get('fingerprint', '')[:12]} "
        f"refreshed={summary.get('refreshed')} "
        f"forecasts={counts.get('forecasts', 0)} "
        f"due={counts.get('with_due_processes', 0)} "
        f"uncovered={counts.get('with_uncovered', 0)}",
        f"recorded new={summary.get('recorded_new', 0)} "
        f"existing={summary.get('recorded_existing', 0)} "
        f"alerts={len(summary.get('alerts') or [])} "
        f"suppressed={len(summary.get('suppressed') or [])} "
        f"deferred={len(summary.get('deferred') or [])} "
        f"pruned={summary.get('pruned', 0)}",
    ]
    lines.extend(summary.get("alerts") or [])
    live_work = summary.get("live_work")
    if live_work:
        lines.append(
            f"live-work updated={live_work.get('updated', 0)} "
            f"added={live_work.get('added', 0)} "
            f"stale={live_work.get('stale', 0)} "
            f"matched={live_work.get('matched', 0)}"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------------------------- DB pass

async def _require_tenant(db: AsyncSession, tenant_id: UUID) -> None:
    tenant = await db.scalar(
        select(GraphTenant).where(GraphTenant.id == tenant_id))
    if tenant is None:
        raise ValueError(f"Unknown tenant: {tenant_id}")


async def _require_user(db: AsyncSession, user_id: UUID | None) -> None:
    if user_id is None:
        return
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise ValueError(f"Unknown created_by user: {user_id}")


async def run_forecast_scan(db: AsyncSession, tenant_id: UUID, snapshot: dict, *,
                            now: datetime | None = None,
                            critical_ids=None, horizons=None,
                            previous_fingerprint: str | None = None,
                            sent_at: dict | None = None,
                            created_by: UUID | None = None,
                            prune: bool = False,
                            live_work_items: list[dict] | None = None) -> dict:
    """One scheduler pass: scan, record, watch, optionally prune.

    Idempotent: re-running the same snapshot records nothing new
    (``record_scan`` is idempotent per fingerprint, and the
    fingerprint gate skips an unchanged twin entirely). An
    unchanged twin performs no writes at all.

    ``live_work_items`` (slice 5): real live-work rows (see
    :mod:`app.predictive_continuity.live_work`) refresh the
    snapshot's live-work entities before the scan, and the
    **refreshed** snapshot is the one recorded — so a scan re-fires
    when live work changes even if the snapshot file did not. The
    summary gains a ``live_work`` block (counts + fingerprints
    only). ``None`` means "no live-work refresh" (unchanged
    behaviour).

    Returns a JSON-serializable summary: tenant, fingerprint,
    refreshed flag, scan counts, recorded new/existing counts,
    alert lines, suppressed/deferred counts, pruned count.
    Raises ``ValueError`` for an unknown tenant/creator or a
    malformed snapshot; database errors propagate.
    """
    moment = now or datetime.now(ORG_TZ)
    await _require_tenant(db, tenant_id)
    await _require_user(db, created_by)
    live_work_summary = None
    if live_work_items is not None:
        refresh = refresh_snapshot_live_work(snapshot, live_work_items)
        snapshot = refresh["snapshot"]
        live_work_summary = {
            "updated": len(refresh["updated"]),
            "added": len(refresh["added"]),
            "stale": len(refresh["stale"]),
            "matched": refresh["matched"],
            "fingerprint_before": refresh["fingerprint_before"],
            "fingerprint_after": refresh["fingerprint_after"],
            "changed": refresh["fingerprint_before"] != refresh["fingerprint_after"],
        }
    report = build_scan_report(
        snapshot, now=moment, critical_ids=critical_ids, horizons=horizons,
        previous_fingerprint=previous_fingerprint, sent_at=sent_at,
    )
    summary = {
        "tenant_id": str(tenant_id),
        "fingerprint": report["fingerprint"],
        "refreshed": report["refreshed"],
        "scanned_at": report["scanned_at"],
        "counts": report["counts"],
        "recorded_new": 0,
        "recorded_existing": 0,
        "alerts": report["alerts"],
        "suppressed": report["suppressed"],
        "deferred": report["deferred"],
        "pruned": 0,
        # Live-work refresh (slice 5): counts + fingerprints only;
        # None when the scan ran without live-work items.
        "live_work": live_work_summary,
        # Updated alert sent_at map (fired alerts stamped with the
        # scan moment) — the caller persists it in the state file so
        # cooldown/dedupe work across runs.
        "sent_at": report["sent_at"],
    }
    if not report["refreshed"]:
        return summary
    # Count genuinely new forecasts before recording: fingerprints
    # already stored for this (tenant, root, horizon) are replays.
    from app.predictive_continuity.models import ForecastRecord

    # Entries carry the fingerprint inside the nested forecast dict
    # (same resolution as record_scan); there is no top-level key.
    wanted = {
        (
            entry["root_id"],
            entry["horizon_days"],
            entry.get("fingerprint")
            or (entry.get("forecast") or {}).get("fingerprint"),
        )
        for entry in report["forecasts"]
    }
    if wanted:
        rows = await db.execute(select(
            ForecastRecord.root_id, ForecastRecord.horizon_days,
            ForecastRecord.fingerprint,
        ).where(ForecastRecord.tenant_id == tenant_id))
        have = {(row[0], row[1], row[2]) for row in rows.all()}
        summary["recorded_new"] = len(wanted - have)
        summary["recorded_existing"] = len(wanted & have)
    scan_result = {
        "fingerprint": report["fingerprint"],
        "scanned_at": report["scanned_at"],
        "forecasts": report["forecasts"],
    }
    await record_scan(db, tenant_id, scan_result, snapshot,
                      created_by=created_by, now=moment)
    if prune:
        summary["pruned"] = await prune_forecasts(db, tenant_id, now=moment)
    await db.commit()
    return summary
