"""CLI entry point for the scheduled drill-reconciliation cycle (Step K v2 slice 6).

Reads one drill run's inputs from JSON files and runs the
reconciliation cycle for a tenant::

    python scripts/twin_reconcile.py --tenant <uuid> --drill-run-id <uuid> \
        --simulation simulation.json --drill-observation drill.json \
        [--g-comparison g.json] [--scenario-kind server_outage]

``--dry-run`` runs the pure comparison only (no database) and
prints the report. Otherwise ``DATABASE_URL`` must point at the
application database (never a live database you have not been
told to use — this CLI writes review-queue rows).

Exit codes: 0 ran (or skipped: no new drill run /
already reconciled — the reason is printed), 2 bad input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

# Pure imports only at module level, so --dry-run works without a
# database driver. The DB path imports sqlalchemy lazily inside main().
from app.digital_twin import reconciliation


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="graph tenant UUID")
    parser.add_argument("--drill-run-id", required=True, help="drill run UUID")
    parser.add_argument("--simulation", required=True, help="run_whatif result JSON file")
    parser.add_argument("--drill-observation", required=True, help="twin-shape drill JSON file")
    parser.add_argument("--g-comparison", default=None, help="Step G drill_comparison JSON file")
    parser.add_argument("--scenario-kind", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="pure comparison only; write nothing")
    return parser.parse_args(argv)


def _bundle(args):
    try:
        tenant_id = UUID(args.tenant)
        drill_run_id = UUID(args.drill_run_id)
    except ValueError as exc:
        raise SystemExit(f"bad UUID: {exc}") from exc
    simulation = _load_json(args.simulation)
    drill_observation = _load_json(args.drill_observation)
    g_comparison = _load_json(args.g_comparison) if args.g_comparison else None
    return tenant_id, {
        "simulation": simulation,
        "drill_observation": drill_observation,
        "g_comparison": g_comparison,
        "drill_run_id": drill_run_id,
        "scenario_kind": args.scenario_kind,
    }


def _dry_run(args):
    tenant_id, bundle = _bundle(args)
    report = reconciliation.reconcile(
        bundle["simulation"], bundle["drill_observation"],
        tenant_id=tenant_id, drill_run_id=bundle["drill_run_id"],
        g_comparison=bundle["g_comparison"], scenario_kind=bundle["scenario_kind"],
    )
    print(reconciliation.reconciliation_summary(report))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


async def _run_cycle(args):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.session import test_engine_options
    from app.digital_twin import reconciliation_schedule

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required (unless --dry-run).")
    tenant_id, bundle = _bundle(args)
    engine = create_async_engine(database_url, poolclass=NullPool, **test_engine_options())
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as db:
            async def provider():
                return bundle

            result = await reconciliation_schedule.run_reconciliation_cycle(
                db, tenant_id, input_provider=provider)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def main(argv=None):
    args = _parse_args(argv)
    if args.dry_run:
        return _dry_run(args)
    result = asyncio.run(_run_cycle(args))
    print(reconciliation.reconciliation_summary(result) if result.get("ran")
          else f"skipped: {result.get('reason')}")
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
